from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mcd_agent.config import (
    AgentConfig,
    check_profile_drift_with_mcc,
    load_config,
    recover_config_from_mcc,
    runtime_effective_map,
    upsert_runtime_values,
)
from mcd_agent.backup import backup_lock_active, backup_profile_sync_from_config, backup_run, backup_status
from mcd_agent.custom_scripts import cached_custom_manifest_keys, cleanup_custom_cache, fetch_custom_manifest
from mcd_agent.db import MauticDB
from mcd_agent.db_watchdog import collect_db_watchdog_snapshot, effective_db_watchdog_config
from mcd_agent.executor import render_mautic_command
from mcd_agent.fs_permissions import ensure_instance_permissions
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.mautic6_core_patch import ensure_m6_plugin_update_metadata_patch, should_apply_m6_plugin_update_metadata_patch
from mcd_agent.pagehit_cascade_patch import ensure_pagehit_cascade_patch
from mcd_agent.runtime_overrides import (
    apply_remote_overrides,
    consume_poll_trigger,
    fetch_runtime_overrides,
    local_runtime_overrides,
    overrides_fingerprint,
    push_runtime_overrides,
)
from mcd_agent.service_profiles import service_profiles_apply_once
from mcd_agent.self_update import maybe_auto_update
from mcd_agent.signals import collect_signals
from mcd_agent.state_push import (
    MCCStatePusher,
    clear_pending_profile_event,
    log_push_result,
    prune_sent_profile_events,
    queue_profile_event,
    read_pending_profile_event,
    should_poll_alert,
)
from mcd_agent.state_backend import (
    ensure_mysql_state_schema,
    mysql_state_connection,
    mysql_state_enabled,
    mysql_state_table_names,
    normalized_state_backend,
)

_CMD_SEP = "\x1f"
_M4_CAMPAIGN_DELETED_CLAUSE_RE = re.compile(r"\s+AND\s*\(?\s*c\.deleted\s+IS\s+NULL\s*\)?", re.IGNORECASE)
_SEGMENT_STALE_PRIORITY_SEC = 24 * 3600
_SEGMENT_STUCK_SPILLOVER_SEC = 2 * 3600
_DB_DISPATCH_PAUSE_SEC = 120
_DB_WATCHDOG_LONG_QUERIES_PAUSE_THRESHOLD = 50
_DB_WATCHDOG_METADATA_LOCKS_PAUSE_THRESHOLD = 10
_ENTITY_LAUNCH_GUARD: dict[str, float] = {}
_SQL_SEGMENTS_ALL_PUBLISHED = (
    "SELECT ll.id "
    "FROM {prefix}lead_lists ll "
    "WHERE ll.is_published = 1 "
    "ORDER BY COALESCE(ll.last_built_date, '1970-01-01 00:00:00') ASC, ll.id ASC"
)

_BACKUP_STABLE_RUNTIME_KEYS = {
    "backup_enabled",
    "backup_dump_timeout_sec",
    "backup_schedule_enabled",
    "backup_schedule_interval_sec",
    "backup_schedule_quiet_hour",
    "backup_schedule_quiet_window_min",
    "backup_schedule_pre_pause_sec",
    "backup_mydumper_threads",
    "backup_mydumper_long_query_guard",
    "backup_mydumper_kill_long_queries",
    "backup_mydumper_extra_args",
    "backup_mydumper_use_nice",
    "backup_mydumper_nice_level",
    "backup_mydumper_use_ionice",
    "backup_mydumper_ionice_class",
    "backup_mydumper_ionice_level",
}

_DB_DISPATCH_PAUSE_ERROR_RE = re.compile(
    r"(too many connections|lost connection to mysql|mysql server has gone away|"
    r"lock wait timeout|deadlock found|metadata lock|\(1040,|\(1205,|\(1213,|\(2006,|\(2013,)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SQLSegmentRule:
    segment_id: int
    select_sql: str
    depends_on: tuple[int, ...]


def _to_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _persist_stable_backup_runtime_to_config(config: AgentConfig, applied_keys: list[str]) -> None:
    stable_keys = sorted(set(applied_keys) & _BACKUP_STABLE_RUNTIME_KEYS)
    if not stable_keys:
        return
    eff_runtime = runtime_effective_map(config)
    updates: dict[str, object] = {}
    for key in stable_keys:
        if key in eff_runtime:
            updates[key] = eff_runtime[key]
    if not updates:
        return
    try:
        path, changed = upsert_runtime_values(config.config_file_path, updates)
        if changed:
            logging.info(
                "runtime-overrides persisted to config (%s): keys=%s",
                path,
                ",".join(stable_keys),
            )
    except Exception as e:
        logging.warning("runtime-overrides persist to config failed: %s", e)


def _to_int_list(value: object) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            iv = _to_int(item)
            if iv is not None:
                out.append(iv)
        return out
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
                return _to_int_list(parsed)
            except Exception:
                pass
        out2: list[int] = []
        for part in raw.split(","):
            iv = _to_int(part)
            if iv is not None:
                out2.append(iv)
        return out2
    iv = _to_int(value)
    return [iv] if iv is not None else []


def _parse_sql_segment_rules(raw: object) -> dict[int, SQLSegmentRule]:
    items: list[tuple[int | None, dict[str, object]]] = []
    if isinstance(raw, dict):
        wrapped = raw.get("rules")
        if isinstance(wrapped, list):
            for item in wrapped:
                if isinstance(item, dict):
                    items.append((None, item))
        else:
            for rk, rv in raw.items():
                if not isinstance(rv, dict):
                    continue
                items.append((_to_int(rk), rv))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                items.append((None, item))

    out: dict[int, SQLSegmentRule] = {}
    for key_sid, item in items:
        enabled_raw = item.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() not in {"0", "false", "off", "no"}
        else:
            enabled = bool(enabled_raw)
        if not enabled:
            continue

        sid = _to_int(item.get("segment_id"))
        if sid is None:
            sid = _to_int(item.get("id"))
        if sid is None:
            sid = key_sid
        if sid is None or sid <= 0:
            continue

        sql_text = (
            str(item.get("select_sql") or item.get("sql") or item.get("query") or "").strip()
        )
        if not sql_text:
            continue

        deps = _to_int_list(
            item.get("depends_on", item.get("dependencies", item.get("deps", item.get("requires"))))
        )
        deps = [x for x in deps if x > 0 and x != sid]
        out[sid] = SQLSegmentRule(
            segment_id=sid,
            select_sql=sql_text,
            depends_on=tuple(dict.fromkeys(deps)),
        )
    return out


def _toposort_sql_segment_ids(ids: list[int], rules: dict[int, SQLSegmentRule]) -> list[int]:
    uniq = list(dict.fromkeys([x for x in ids if x in rules]))
    if not uniq:
        return []
    active = set(uniq)
    indeg: dict[int, int] = {sid: 0 for sid in uniq}
    outgoing: dict[int, set[int]] = {sid: set() for sid in uniq}
    for sid in uniq:
        for dep in rules[sid].depends_on:
            if dep not in active:
                continue
            # dep -> sid
            if sid not in outgoing[dep]:
                outgoing[dep].add(sid)
                indeg[sid] += 1
    queue = sorted([sid for sid in uniq if indeg[sid] == 0])
    ordered: list[int] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for nxt in sorted(outgoing.get(current, set())):
            indeg[nxt] = max(0, indeg[nxt] - 1)
            if indeg[nxt] == 0 and nxt not in queue and nxt not in ordered:
                queue.append(nxt)
    if len(ordered) == len(uniq):
        return ordered
    # Keep deterministic order on dependency cycles/malformed graphs.
    tail = [sid for sid in sorted(uniq) if sid not in set(ordered)]
    return ordered + tail


def _plan_sql_segment_ring(due_ids: list[int], rules: dict[int, SQLSegmentRule]) -> list[int]:
    due_set = set(dict.fromkeys(due_ids))
    managed_due = [sid for sid in due_ids if sid in rules]
    if not managed_due:
        return []
    planned: set[int] = set(managed_due)
    queue = deque(managed_due)
    while queue:
        sid = queue.popleft()
        rule = rules.get(sid)
        if rule is None:
            continue
        for dep in rule.depends_on:
            if dep not in rules:
                continue
            # Always include SQL-managed dependencies for deterministic order.
            if dep not in planned:
                planned.add(dep)
                queue.append(dep)
    ordered = _toposort_sql_segment_ids(list(planned), rules)
    # Keep due IDs first where possible; dependencies not due are still kept.
    ordered_due = [sid for sid in ordered if sid in due_set]
    ordered_deps = [sid for sid in ordered if sid not in due_set]
    return ordered_due + ordered_deps


def _segment_sql_state_key(root: str, segment_id: int) -> str:
    root_hash = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:24]
    return f"segment_sql_state:{root_hash}:{int(segment_id)}"


def _segment_sql_state_load(store: "TaskStore", root: str, segment_id: int) -> dict[str, object]:
    payload = store.get_runtime_sync(_segment_sql_state_key(root, segment_id))
    return dict(payload) if isinstance(payload, dict) else {}


def _segment_sql_rule_kind(rule: SQLSegmentRule) -> str:
    sql = str(rule.select_sql or "")
    if re.search(r"\bpage_hits\b", sql, flags=re.IGNORECASE):
        return "page_hits"
    return "generic"


def _in_daily_quiet_window(now_local: datetime, quiet_hour: int, quiet_window_min: int) -> bool:
    start = now_local.replace(hour=max(0, min(23, int(quiet_hour))), minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=max(1, int(quiet_window_min)))
    return start <= now_local < end


def _segment_sql_try_acquire(
    *,
    store: "TaskStore",
    config: AgentConfig,
    root: str,
    segment_id: int,
    owner: str,
    rule_kind: str,
    now_ts: float,
) -> tuple[bool, dict[str, object]]:
    payload = _segment_sql_state_load(store, root, segment_id)
    status = str(payload.get("status") or "").strip().lower()
    prev_owner = str(payload.get("owner") or "").strip()
    last_started_at = float(payload.get("last_started_at") or 0.0)
    last_heartbeat_at = float(payload.get("heartbeat_at") or payload.get("started_at") or 0.0)
    min_repeat = max(0, int(getattr(config, "segment_sql_min_repeat_sec", 0) or 0))
    orphan_after_sec = max(30, int(getattr(config, "segment_sql_orphan_after_sec", 900) or 900))
    orphan_policy = str(getattr(config, "segment_sql_orphan_policy", "reclaim_stale") or "reclaim_stale").strip().lower()
    stale_running = (
        status == "running"
        and prev_owner
        and prev_owner != owner
        and last_heartbeat_at > 0
        and (float(now_ts) - float(last_heartbeat_at)) >= float(orphan_after_sec)
    )
    if status == "running" and prev_owner and prev_owner != owner:
        if not stale_running or orphan_policy != "reclaim_stale":
            retry_after = 0
            if stale_running:
                retry_after = 0
            elif last_heartbeat_at > 0:
                retry_after = max(1, int(orphan_after_sec - (float(now_ts) - float(last_heartbeat_at))))
            return False, {
                **payload,
                "reason": "running_locked",
                "retry_after_sec": retry_after,
            }

    if last_started_at > 0 and min_repeat > 0 and (float(now_ts) - float(last_started_at)) < float(min_repeat):
        return False, {
            **payload,
            "reason": "cooldown",
            "retry_after_sec": max(1, int(float(min_repeat) - (float(now_ts) - float(last_started_at)))),
        }

    next_payload = dict(payload)
    next_payload.update(
        {
            "root": str(root),
            "segment_id": int(segment_id),
            "status": "running",
            "owner": str(owner),
            "rule_kind": str(rule_kind),
            "started_at": float(now_ts),
            "last_started_at": float(now_ts),
            "heartbeat_at": float(now_ts),
            "orphan_policy": orphan_policy,
            "orphan_after_sec": int(orphan_after_sec),
            "cooldown_sec": int(min_repeat),
        }
    )
    if stale_running:
        next_payload["reclaimed_from_owner"] = prev_owner
        next_payload["reclaimed_at"] = float(now_ts)
    store.put_runtime_sync(_segment_sql_state_key(root, segment_id), next_payload)
    return True, next_payload


def _segment_sql_heartbeat(
    *,
    store: "TaskStore",
    root: str,
    segment_id: int,
    owner: str,
    now_ts: float,
) -> None:
    payload = _segment_sql_state_load(store, root, segment_id)
    if str(payload.get("status") or "").strip().lower() != "running":
        return
    if str(payload.get("owner") or "").strip() != str(owner):
        return
    payload["heartbeat_at"] = float(now_ts)
    store.put_runtime_sync(_segment_sql_state_key(root, segment_id), payload)


def _segment_sql_finish(
    *,
    store: "TaskStore",
    root: str,
    segment_id: int,
    owner: str,
    now_ts: float,
    result: str,
    note: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload = _segment_sql_state_load(store, root, segment_id)
    if str(payload.get("owner") or "").strip() != str(owner) and payload:
        return
    payload.update(
        {
            "root": str(root),
            "segment_id": int(segment_id),
            "status": "idle",
            "owner": "",
            "heartbeat_at": float(now_ts),
            "last_finished_at": float(now_ts),
            "last_result": str(result),
            "last_note": str(note or "")[:1000],
        }
    )
    if extra:
        payload.update(extra)
    store.put_runtime_sync(_segment_sql_state_key(root, segment_id), payload)


def _segment_sql_start_heartbeat(
    *,
    store: "TaskStore",
    root: str,
    segment_id: int,
    owner: str,
    interval_sec: int,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _worker() -> None:
        while not stop_event.wait(max(5, int(interval_sec))):
            try:
                _segment_sql_heartbeat(
                    store=store,
                    root=root,
                    segment_id=segment_id,
                    owner=owner,
                    now_ts=time.time(),
                )
            except Exception as e:
                logging.warning("[%s] segment_sql heartbeat failed id=%s: %s", root, segment_id, e)

    thread = threading.Thread(
        target=_worker,
        name=f"segment-sql-heartbeat-{segment_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _run_sql_segment_ring(
    *,
    config: AgentConfig,
    store: "TaskStore",
    db: MauticDB,
    root: str,
    ring: deque[int],
    rules: dict[int, SQLSegmentRule],
    active_set: set[int],
    done_set: set[int],
    running: dict[str, "RunningTask"],
    sql_ctx: dict[str, str],
    now_ts: float,
    now_local: datetime,
) -> int:
    if not config.segment_sql_ring_enabled:
        return 0
    limit = max(0, int(getattr(config, "segment_sql_ring_max_per_tick", 1) or 0))
    if limit <= 0 or not ring or not rules:
        return 0
    launched = 0
    scans = len(ring)
    while scans > 0 and launched < limit:
        sid = int(ring[0])
        scans -= 1
        rule = rules.get(sid)
        if rule is None:
            ring.popleft()
            active_set.discard(sid)
            done_set.discard(sid)
            continue
        rule_kind = _segment_sql_rule_kind(rule)
        if (
            rule_kind == "page_hits"
            and bool(getattr(config, "segment_sql_page_hits_quiet_only", False))
            and not _in_daily_quiet_window(
                now_local,
                int(getattr(config, "segment_sql_page_hits_quiet_hour", 2) or 2),
                int(getattr(config, "segment_sql_page_hits_quiet_window_min", 180) or 180),
            )
        ):
            ring.rotate(-1)
            continue
        if _is_running(running, root, "segment", sid):
            ring.rotate(-1)
            continue
        if not _launch_allowed(config, root, "segment_sql", sid, now_ts=now_ts):
            ring.rotate(-1)
            continue
        dep_wait = [dep for dep in rule.depends_on if dep in active_set and dep not in done_set]
        if dep_wait:
            ring.rotate(-1)
            continue
        owner = f"{_state_node_id(config)}:{os.getpid()}:{sid}:{int(now_ts)}"
        lock_ok, _lock_state = _segment_sql_try_acquire(
            store=store,
            config=config,
            root=root,
            segment_id=sid,
            owner=owner,
            rule_kind=rule_kind,
            now_ts=now_ts,
        )
        if not lock_ok:
            ring.rotate(-1)
            continue
        hb_stop, hb_thread = _segment_sql_start_heartbeat(
            store=store,
            root=root,
            segment_id=sid,
            owner=owner,
            interval_sec=int(getattr(config, "segment_sql_lock_heartbeat_sec", 15) or 15),
        )
        try:
            res = db.rebuild_segment_membership(
                segment_id=sid,
                select_query_template=rule.select_sql,
                context=sql_ctx,
            )
            done_set.add(sid)
            _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, "segment_sql", sid)] = float(now_ts)
            # Keep shared cooldown in sync with regular `segment` task type.
            _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, "segment", sid)] = float(now_ts)
            _segment_sql_finish(
                store=store,
                root=root,
                segment_id=sid,
                owner=owner,
                now_ts=time.time(),
                result="ok",
                note="rebuilt",
                extra={
                    "selected_count": int(res.get("selected_count", 0) or 0),
                    "deleted_count": int(res.get("deleted_count", 0) or 0),
                    "inserted_count": int(res.get("inserted_count", 0) or 0),
                    "duration_sec": float(res.get("duration_sec", 0.0) or 0.0),
                },
            )
            ring.rotate(-1)
            launched += 1
            logging.info(
                "[%s] segment_sql rebuilt id=%s selected=%s inserted=%s deleted=%s duration=%.2fs",
                root,
                sid,
                int(res.get("selected_count", 0) or 0),
                int(res.get("inserted_count", 0) or 0),
                int(res.get("deleted_count", 0) or 0),
                float(res.get("duration_sec", 0.0) or 0.0),
            )
        except Exception as e:
            ring.rotate(-1)
            _segment_sql_finish(
                store=store,
                root=root,
                segment_id=sid,
                owner=owner,
                now_ts=time.time(),
                result="error",
                note=str(e),
            )
            logging.warning("[%s] segment_sql rebuild failed id=%s: %s", root, sid, e)
        finally:
            hb_stop.set()
            hb_thread.join(timeout=2.0)
    return launched


def _backup_done_for_local_date(config: AgentConfig, local_dt: datetime) -> bool:
    today = local_dt.strftime("%Y-%m-%d")
    try:
        st = backup_status(config)
    except Exception:
        return False

    if str(st.get("last_status") or "").strip().lower() != "ok":
        return False

    # Preferred source of truth for daily success.
    last_success_at = str(st.get("last_success_at") or "").strip()
    if last_success_at:
        try:
            ts = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone().date() == local_dt.date():
                return True
        except Exception:
            pass

    # Backward-compatible fallback: final path suffix contains date.
    last_path = str(st.get("last_backup_path") or "").strip().rstrip("/")
    return bool(last_path) and last_path.endswith(f"/{today}")


def _backup_attempted_for_local_date(config: AgentConfig, local_dt: datetime) -> bool:
    today = local_dt.strftime("%Y-%m-%d")
    try:
        st = backup_status(config)
    except Exception:
        return False

    # A finished run (ok/failed) for today counts as "attempted" so dispatch
    # can resume after backup completion.
    last_run_at = str(st.get("last_run_at") or "").strip()
    if last_run_at:
        try:
            ts = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts.astimezone().date() == local_dt.date():
                return True
        except Exception:
            pass

    # Fallback for older states where only success path/timestamp may exist.
    if str(st.get("last_status") or "").strip().lower() != "ok":
        return False
    last_path = str(st.get("last_backup_path") or "").strip().rstrip("/")
    if last_path.endswith(f"/{today}"):
        return True
    last_success_at = str(st.get("last_success_at") or "").strip()
    if not last_success_at:
        return False
    try:
        ts = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone().date() == local_dt.date()
    except Exception:
        return False


def _backup_dispatch_pause_state(
    config: AgentConfig,
    *,
    backup_running: bool,
    now_local: datetime | None = None,
) -> tuple[bool, str]:
    if not config.backup_enabled:
        return False, ""
    if backup_running:
        return True, "backup_running"
    if not config.backup_schedule_enabled:
        return False, ""

    pre_pause_sec = max(0, int(config.backup_schedule_pre_pause_sec))
    if pre_pause_sec <= 0:
        return False, ""

    quiet_hour = max(0, min(23, int(config.backup_schedule_quiet_hour)))
    quiet_window_min = max(1, min(180, int(config.backup_schedule_quiet_window_min)))
    dt_local = now_local if now_local is not None else datetime.now()
    start_today = dt_local.replace(hour=quiet_hour, minute=0, second=0, microsecond=0)
    done_today = _backup_attempted_for_local_date(config, dt_local)
    in_window = start_today <= dt_local < (start_today + timedelta(minutes=quiet_window_min))

    # If today's backup is still pending and we are already in backup slot,
    # block new task launches until backup run starts/finishes.
    if in_window and not done_today:
        return True, "backup_window_pending"

    if dt_local < start_today:
        next_start = start_today
    else:
        next_start = start_today + timedelta(days=1)

    sec_to_next = int((next_start - dt_local).total_seconds())
    if 0 < sec_to_next <= pre_pause_sec:
        return True, f"pre_backup_window_{sec_to_next}s"
    return False, ""


@dataclass
class RunningTask:
    row_id: int
    root: str
    task_key: str
    task_type: str
    entity_id: int | None
    command_str: str
    timeout_sec: int
    attempts: int
    started_at: float
    pid: int
    manual_request_id: int | None = None


def _load_id_file(path: str | None) -> set[int]:
    out: set[int] = set()
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    out.add(int(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        logging.warning("ID file not found: %s", path)
    return out


def _extract_int(row: dict[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_node_id(cfg: AgentConfig | None) -> str:
    if cfg is None:
        return "local-node"
    ident = resolve_agent_identity(cfg)
    for key in (
        "local_hostname",
        "effective_hostname",
        "configured_host_name",
        "effective_mcc_host_name",
    ):
        raw = str(ident.get(key) or "").strip()
        if raw:
            return raw[:191]
    return "unknown-host"


def _segment_weights(
    ids: list[int],
    weight_rows: list[dict[str, object]],
    whitelist: set[int],
    now_ts: int,
) -> dict[int, float]:
    by_id: dict[int, dict[str, object]] = {}
    for row in weight_rows:
        rid = _extract_int(row, "id")
        if rid is not None:
            by_id[rid] = row

    weights: dict[int, float] = {}
    for sid in ids:
        row = by_id.get(sid, {})
        created_ts = _extract_int(row, "created_ts")
        modified_ts = _extract_int(row, "modified_ts")
        built_ts = _extract_int(row, "built_ts")
        recent_activity = _extract_int(row, "recent_activity")
        w = 0.0
        if created_ts is not None:
            created_age_h = max(0.0, (now_ts - created_ts) / 3600.0)
            w += max(0.0, 400.0 - created_age_h)
        if built_ts is None:
            w += 80.0
        else:
            staleness_h = max(0.0, (now_ts - built_ts) / 3600.0)
            w += min(40.0, staleness_h)

        if modified_ts is not None and (built_ts is None or modified_ts > built_ts):
            w += 120.0
        if recent_activity is not None and recent_activity > 0:
            w += min(600.0, float(recent_activity) * 2.0)

        if sid in whitelist:
            w += 1000.0

        weights[sid] = w
    return weights


def _campaign_weights(
    ids: list[int],
    weight_rows: list[dict[str, object]],
    whitelist: set[int],
    now_ts: int,
) -> dict[int, float]:
    by_id: dict[int, dict[str, object]] = {}
    for row in weight_rows:
        rid = _extract_int(row, "id")
        if rid is not None:
            by_id[rid] = row

    out: dict[int, float] = {}
    for cid in ids:
        row = by_id.get(cid, {})
        publish_ts = _extract_int(row, "publish_ts")
        pending_cnt = _extract_int(row, "pending_cnt")
        recent_activity = _extract_int(row, "recent_activity")
        if publish_ts is None:
            w = 0.0
        else:
            age_h = max(0.0, (now_ts - publish_ts) / 3600.0)
            w = max(0.0, 500.0 - (age_h * 2.0))
        if pending_cnt is not None and pending_cnt > 0:
            w += min(800.0, float(pending_cnt) * 0.5)
        if recent_activity is not None and recent_activity > 0:
            w += min(600.0, float(recent_activity) * 2.0)
        if cid in whitelist:
            w += 1000.0
        out[cid] = w
    return out


def _latest_campaign_ids(weight_rows: list[dict[str, object]], count: int) -> list[int]:
    if count <= 0:
        return []
    pairs: list[tuple[int, int]] = []
    for row in weight_rows:
        rid = _extract_int(row, "id")
        publish_ts = _extract_int(row, "publish_ts")
        if rid is None or publish_ts is None:
            continue
        pairs.append((rid, publish_ts))
    pairs.sort(key=lambda x: (-x[1], -x[0]))
    return [rid for rid, _ in pairs[:count]]


def _latest_campaign_ids_from_ids(ids: list[int], count: int) -> list[int]:
    if count <= 0:
        return []
    uniq = list(dict.fromkeys(ids))
    return sorted(uniq, reverse=True)[:count]


def _split_segment_circles(
    ids: list[int],
    weights: dict[int, float],
    whitelist: set[int],
    threshold: float,
    priority_size: int,
    stale_priority_ids: set[int] | None = None,
) -> tuple[list[int], list[int]]:
    uniq = list(dict.fromkeys(ids))
    stale_set = set(stale_priority_ids or set())
    priority_set: set[int] = set(whitelist)
    if stale_priority_ids:
        priority_set.update(stale_priority_ids)
    ranked = sorted(uniq, key=lambda x: (-weights.get(x, 0.0), x))
    for sid in ranked[: max(0, priority_size)]:
        priority_set.add(sid)
    for sid in uniq:
        if weights.get(sid, 0.0) >= threshold:
            priority_set.add(sid)

    def _prio_key(sid: int) -> tuple[int, int, float, int]:
        # Priority order inside ring:
        # 1) whitelist (forced),
        # 2) stale (>24h or never built),
        # 3) computed weight,
        # 4) id for deterministic tie-break.
        return (
            0 if sid in whitelist else 1,
            0 if sid in stale_set else 1,
            -weights.get(sid, 0.0),
            sid,
        )

    priority = sorted([x for x in uniq if x in priority_set], key=_prio_key)
    regular = sorted([x for x in uniq if x not in priority_set], key=lambda x: (-weights.get(x, 0.0), x))
    return priority, regular


def _stale_segment_priority_ids(
    ids: list[int],
    weight_rows: list[dict[str, object]],
    now_ts: int,
    stale_sec: int = _SEGMENT_STALE_PRIORITY_SEC,
) -> set[int]:
    uniq = list(dict.fromkeys(ids))
    by_id_built_ts: dict[int, int | None] = {}
    for row in weight_rows:
        rid = _extract_int(row, "id")
        if rid is None:
            continue
        by_id_built_ts[rid] = _extract_int(row, "built_ts")

    out: set[int] = set()
    limit_sec = max(60, int(stale_sec))
    for sid in uniq:
        built_ts = by_id_built_ts.get(sid)
        # Never built / unknown built date => treat as stale.
        if built_ts is None:
            out.add(sid)
            continue
        if now_ts - built_ts >= limit_sec:
            out.add(sid)
    return out


def _split_campaign_circles(
    ids: list[int],
    weights: dict[int, float],
    whitelist: set[int],
    priority_size: int,
    latest_priority_ids: list[int],
) -> tuple[list[int], list[int]]:
    uniq = list(dict.fromkeys(ids))
    latest_set = set(latest_priority_ids)
    # For equal weights, prefer newer campaign IDs first.
    wl_sorted = sorted([x for x in uniq if x in whitelist], key=lambda x: (-weights.get(x, 0.0), -x))
    latest_sorted = sorted([x for x in uniq if x in latest_set and x not in whitelist], key=lambda x: (-weights.get(x, 0.0), -x))
    non_wl = sorted([x for x in uniq if x not in whitelist], key=lambda x: (-weights.get(x, 0.0), -x))

    forced = wl_sorted + latest_sorted
    forced_set = set(forced)
    need = max(0, priority_size - len(forced))
    picked_non_wl = [x for x in non_wl if x not in forced_set][:need]
    priority_set = set(forced + picked_non_wl)

    priority = [x for x in (forced + picked_non_wl) if x in uniq]
    regular = [x for x in non_wl if x not in priority_set]
    return priority, regular


def _needs_weight_recalc(ids: list[int], cached: dict[int, float]) -> bool:
    if not cached:
        return True
    return set(ids) != set(cached.keys())


def _reconcile_ring(old_ring: deque[int] | None, ordered_ids: list[int]) -> deque[int]:
    if old_ring is None or not old_ring:
        return deque(ordered_ids)
    old = list(old_ring)
    new_set = set(ordered_ids)
    keep = [x for x in old if x in new_set]
    keep_set = set(keep)
    tail = [x for x in ordered_ids if x not in keep_set]
    return deque(keep + tail)


def _mark_ring_entity_executed(ring: deque[int], entity_id: int | None) -> None:
    if entity_id is None or not ring:
        return
    try:
        ring.remove(int(entity_id))
        ring.append(int(entity_id))
    except ValueError:
        return


def _partition_complete(ids: list[int], p1: list[int], p2: list[int]) -> bool:
    base = set(ids)
    return base == (set(p1) | set(p2))


def _campaign_sql_for_major(query_template: str, mautic_major: int | None) -> str:
    if mautic_major != 4:
        return query_template
    # Mautic 4 schemas may not have campaigns.deleted; strip this predicate safely.
    patched = _M4_CAMPAIGN_DELETED_CLAUSE_RE.sub("", query_template)
    if patched == query_template:
        return query_template
    return re.sub(r"\s{2,}", " ", patched).strip()


def _is_db_dispatch_pause_error(exc: Exception) -> bool:
    return bool(_DB_DISPATCH_PAUSE_ERROR_RE.search(str(exc or "")))


def _mark_db_dispatch_pause(
    *,
    root: str,
    reason: str,
    now_ts: float,
    pause_until: dict[str, float],
    pause_reasons: dict[str, str],
    pause_sec: int = _DB_DISPATCH_PAUSE_SEC,
) -> None:
    until = now_ts + float(max(30, int(pause_sec)))
    prev_until = float(pause_until.get(root, 0.0))
    prev_reason = pause_reasons.get(root, "")
    pause_until[root] = max(prev_until, until)
    pause_reasons[root] = reason
    if prev_until <= now_ts or prev_reason != reason:
        logging.warning(
            "[%s] db dispatch circuit-breaker active for %ss: %s",
            root,
            int(max(30, int(pause_sec))),
            reason,
        )


def _clear_campaign_rings(
    *,
    root: str,
    trigger_prio_rings: dict[str, deque[int]],
    trigger_reg_rings: dict[str, deque[int]],
    rebuild_prio_rings: dict[str, deque[int]],
    rebuild_reg_rings: dict[str, deque[int]],
    trigger_prio_sets: dict[str, set[int]],
    trigger_reg_sets: dict[str, set[int]],
    rebuild_prio_sets: dict[str, set[int]],
    rebuild_reg_sets: dict[str, set[int]],
) -> None:
    trigger_prio_rings[root] = deque()
    trigger_reg_rings[root] = deque()
    rebuild_prio_rings[root] = deque()
    rebuild_reg_rings[root] = deque()
    trigger_prio_sets[root] = set()
    trigger_reg_sets[root] = set()
    rebuild_prio_sets[root] = set()
    rebuild_reg_sets[root] = set()


def _fill_from_ring(
    *,
    ring: deque[int],
    ring_limit: int,
    total_limit: int,
    root: str,
    task_type: str,
    running: dict[str, RunningTask],
    ring_entities: set[int] | None,
    config: AgentConfig,
    store: TaskStore,
    popens: dict[str, subprocess.Popen[bytes]],
    build_args,
) -> None:
    if not ring or ring_limit <= 0 or total_limit <= 0:
        return
    scans = len(ring)
    while _running_count_for_entities(running, root, task_type, ring_entities) < ring_limit and scans > 0:
        eid = ring[0]
        scans -= 1
        if _is_running(running, root, task_type, eid):
            ring.rotate(-1)
            continue
        if not _launch_allowed(config, root, task_type, eid):
            ring.rotate(-1)
            continue
        args = build_args(eid)
        launched = _submit_if_slot(
            config=config,
            store=store,
            running=running,
            root=root,
            task_type=task_type,
            entity_id=eid,
            args=args,
            timeout_sec=config.command_timeout_sec,
            max_parallel_for_type=max(1, total_limit),
            popens=popens,
        )
        if launched:
            ring.rotate(-1)
        else:
            break


def _compute_throttle_active(samples: deque[tuple[float, int]], threshold: int, window_min: int) -> bool:
    if not samples:
        return False
    now = time.time()
    window_sec = max(60, window_min * 60)
    while samples and now - samples[0][0] > window_sec:
        samples.popleft()
    if not samples:
        return False
    if now - samples[0][0] < window_sec:
        return False
    return min(v for _, v in samples) >= threshold


class TaskStore:
    def __init__(self, path: str, cfg: AgentConfig | None = None) -> None:
        self.path = path
        self.cfg = cfg
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        try:
            self.conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        self._init_sqlite_schema()
        self._node_id = _state_node_id(cfg)

        self._mysql_mode = bool(
            cfg is not None
            and normalized_state_backend(cfg) == "mysql_hybrid"
            and mysql_state_enabled(cfg)
        )
        self._mysql_tables = mysql_state_table_names(cfg) if self._mysql_mode and cfg is not None else {}
        self._mysql_conn = None
        self._mysql_retry_after_ts = 0.0
        if self._mysql_mode:
            if self.cfg is not None:
                try:
                    ensure_mysql_state_schema(self.cfg)
                except Exception:
                    self._mysql_mark_error()
            if self._mysql_available(force_probe=True) and self._migrate_sqlite_to_mysql_once():
                self._sqlite_prune_for_failover()

    def _init_sqlite_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              root TEXT NOT NULL,
              task_key TEXT NOT NULL,
              task_type TEXT NOT NULL,
              entity_id INTEGER,
              command_str TEXT NOT NULL,
              pid INTEGER NOT NULL,
              timeout_sec INTEGER NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 1,
              manual_request_id INTEGER,
              state TEXT NOT NULL,
              note TEXT,
              started_at REAL NOT NULL,
              finished_at REAL,
              rc INTEGER
            )
            """
        )
        self._ensure_sqlite_column("tasks", "manual_request_id", "ALTER TABLE tasks ADD COLUMN manual_request_id INTEGER")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_running ON tasks(state, root, task_type)")
        self.conn.execute("DROP INDEX IF EXISTS idx_tasks_task_key_running")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weight_cache (
              kind TEXT NOT NULL,
              root TEXT NOT NULL,
              entity_id INTEGER NOT NULL,
              weight REAL NOT NULL,
              computed_at REAL NOT NULL,
              PRIMARY KEY(kind, root, entity_id)
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_weight_cache_lookup ON weight_cache(kind, root, computed_at)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_sync (
              key TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              root TEXT NOT NULL,
              task_type TEXT NOT NULL,
              entity_id INTEGER,
              command_str TEXT NOT NULL,
              timeout_sec INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              note TEXT,
              task_key TEXT,
              requested_at REAL NOT NULL,
              launched_at REAL,
              finished_at REAL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_requests_pending ON manual_requests(status, root, requested_at)"
        )
        self.conn.commit()

    def _ensure_sqlite_column(self, table: str, column: str, ddl: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        cols = {str(r["name"]) for r in cur.fetchall()}
        if column not in cols:
            self.conn.execute(ddl)

    def _mysql_mark_error(self) -> None:
        self._mysql_retry_after_ts = time.time() + 15.0
        try:
            if self._mysql_conn is not None:
                self._mysql_conn.close()
        except Exception:
            pass
        self._mysql_conn = None

    def _mysql_available(self, *, force_probe: bool = False) -> bool:
        if not self._mysql_mode or self.cfg is None:
            return False
        now = time.time()
        if not force_probe and now < self._mysql_retry_after_ts:
            return False
        if self._mysql_conn is not None:
            try:
                self._mysql_conn.ping(reconnect=True)
                return True
            except Exception:
                self._mysql_mark_error()
        try:
            self._mysql_conn = mysql_state_connection(self.cfg)
            self._mysql_retry_after_ts = 0.0
            return True
        except Exception:
            self._mysql_mark_error()
            return False

    def _mysql_query(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        if not self._mysql_available():
            raise RuntimeError("mysql_unavailable")
        assert self._mysql_conn is not None
        try:
            with self._mysql_conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
                return [dict(r) for r in rows if isinstance(r, dict)]
        except Exception:
            self._mysql_mark_error()
            raise

    def _mysql_exec(self, sql: str, params: tuple[object, ...] = ()) -> tuple[int, int]:
        if not self._mysql_available():
            raise RuntimeError("mysql_unavailable")
        assert self._mysql_conn is not None
        try:
            with self._mysql_conn.cursor() as cur:
                cur.execute(sql, params)
                return int(cur.lastrowid or 0), int(cur.rowcount or 0)
        except Exception:
            self._mysql_mark_error()
            raise

    def _mysql_exec_many(self, sql: str, params: list[tuple[object, ...]]) -> int:
        if not params:
            return 0
        if not self._mysql_available():
            raise RuntimeError("mysql_unavailable")
        assert self._mysql_conn is not None
        try:
            with self._mysql_conn.cursor() as cur:
                cur.executemany(sql, params)
                return int(cur.rowcount or 0)
        except Exception:
            self._mysql_mark_error()
            raise

    def _sqlite_fetchall_dicts(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _sqlite_prune_for_failover(self) -> None:
        """In mysql mode keep only minimum local fallback footprint."""
        if not self._mysql_mode:
            return
        try:
            self.conn.execute("DELETE FROM tasks WHERE state!='running'")
            self.conn.execute("DELETE FROM manual_requests WHERE status NOT IN ('pending','launched')")
            self.conn.execute("DELETE FROM weight_cache")
            # Keep runtime_sync only for migration marker and local runtime hints.
            self.conn.execute(
                """
                DELETE FROM runtime_sync
                WHERE key NOT IN ('taskstore_mysql_migrated_v1', 'local_runtime', 'mcc_runtime')
                """
            )
            self.conn.commit()
        except Exception:
            pass

    def _migrate_sqlite_to_mysql_once(self) -> bool:
        if not self._mysql_mode:
            return False
        marker = self.conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key='taskstore_mysql_migrated_v1' LIMIT 1"
        ).fetchone()
        if marker:
            raw = str(marker["payload_json"] or "")
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}
            if str(payload.get("status") or "").strip().lower() == "ok":
                return True
        if not self._mysql_available():
            return False

        tasks_table = self._mysql_tables.get("tasks", "")
        weight_table = self._mysql_tables.get("weight_cache", "")
        runtime_table = self._mysql_tables.get("runtime_sync", "")
        req_table = self._mysql_tables.get("manual_requests", "")
        if not all([tasks_table, weight_table, runtime_table, req_table]):
            return False

        migrated = {"tasks": 0, "weights": 0, "runtime_sync": 0, "manual_requests": 0}
        try:
            # tasks
            rows = self._sqlite_fetchall_dicts(
                """
                SELECT id, root, task_key, task_type, entity_id, command_str, pid, timeout_sec,
                       attempts, manual_request_id, state, note, started_at, finished_at, rc
                FROM tasks
                ORDER BY id ASC
                """
            )
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{tasks_table}`(
                      host_name, root, task_key, task_type, entity_id, command_str, pid, timeout_sec,
                      attempts, manual_request_id, state, note, started_at, finished_at, rc
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      host_name=VALUES(host_name),
                      root=VALUES(root),
                      task_key=VALUES(task_key),
                      task_type=VALUES(task_type),
                      entity_id=VALUES(entity_id),
                      command_str=VALUES(command_str),
                      pid=VALUES(pid),
                      timeout_sec=VALUES(timeout_sec),
                      attempts=VALUES(attempts),
                      manual_request_id=VALUES(manual_request_id),
                      state=VALUES(state),
                      note=VALUES(note),
                      started_at=VALUES(started_at),
                      finished_at=VALUES(finished_at),
                      rc=VALUES(rc)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["root"] or ""),
                            str(r["task_key"] or ""),
                            str(r["task_type"] or ""),
                            r["entity_id"],
                            str(r["command_str"] or ""),
                            int(r["pid"] or 0),
                            int(r["timeout_sec"] or 0),
                            int(r["attempts"] or 1),
                            r["manual_request_id"],
                            str(r["state"] or ""),
                            r.get("note"),
                            float(r["started_at"] or 0.0),
                            (float(r["finished_at"]) if r.get("finished_at") is not None else None),
                            r.get("rc"),
                        )
                        for r in rows
                    ],
                )
                migrated["tasks"] = len(rows)

            # weight_cache
            rows = self._sqlite_fetchall_dicts(
                "SELECT kind, root, entity_id, weight, computed_at FROM weight_cache"
            )
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{weight_table}`(host_name, kind, root, entity_id, weight, computed_at)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      weight=VALUES(weight),
                      computed_at=VALUES(computed_at)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["kind"] or ""),
                            str(r["root"] or ""),
                            int(r["entity_id"] or 0),
                            float(r["weight"] or 0.0),
                            float(r["computed_at"] or 0.0),
                        )
                        for r in rows
                    ],
                )
                migrated["weights"] = len(rows)

            # runtime_sync
            rows = self._sqlite_fetchall_dicts("SELECT key, payload_json, updated_at FROM runtime_sync")
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{runtime_table}`(host_name, `key`, payload_json, updated_at)
                    VALUES(%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      payload_json=VALUES(payload_json),
                      updated_at=VALUES(updated_at)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["key"] or ""),
                            str(r["payload_json"] or "{}"),
                            float(r["updated_at"] or time.time()),
                        )
                        for r in rows
                    ],
                )
                migrated["runtime_sync"] = len(rows)

            # manual_requests
            rows = self._sqlite_fetchall_dicts(
                """
                SELECT id, root, task_type, entity_id, command_str, timeout_sec, status,
                       note, task_key, requested_at, launched_at, finished_at
                FROM manual_requests
                ORDER BY id ASC
                """
            )
            if rows:
                self._mysql_exec_many(
                    f"""
                    INSERT INTO `{req_table}`(
                      host_name, root, task_type, entity_id, command_str, timeout_sec, status,
                      note, task_key, requested_at, launched_at, finished_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      host_name=VALUES(host_name),
                      root=VALUES(root),
                      task_type=VALUES(task_type),
                      entity_id=VALUES(entity_id),
                      command_str=VALUES(command_str),
                      timeout_sec=VALUES(timeout_sec),
                      status=VALUES(status),
                      note=VALUES(note),
                      task_key=VALUES(task_key),
                      requested_at=VALUES(requested_at),
                      launched_at=VALUES(launched_at),
                      finished_at=VALUES(finished_at)
                    """,
                    [
                        (
                            self._node_id,
                            str(r["root"] or ""),
                            str(r["task_type"] or ""),
                            r["entity_id"],
                            str(r["command_str"] or ""),
                            int(r["timeout_sec"] or 0),
                            str(r["status"] or "pending"),
                            r.get("note"),
                            r.get("task_key"),
                            float(r["requested_at"] or 0.0),
                            (float(r["launched_at"]) if r.get("launched_at") is not None else None),
                            (float(r["finished_at"]) if r.get("finished_at") is not None else None),
                        )
                        for r in rows
                    ],
                )
                migrated["manual_requests"] = len(rows)

            self.conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (
                    "taskstore_mysql_migrated_v1",
                    json.dumps(
                        {"status": "ok", "migrated": migrated, "at": int(time.time())},
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )
            self.conn.commit()
        except Exception as e:
            self.conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (
                    "taskstore_mysql_migrated_v1",
                    json.dumps(
                        {"status": "error", "error": str(e), "at": int(time.time())},
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )
            self.conn.commit()
            return False
        return True

    def mark_old_running_lost(self) -> None:
        now = time.time()
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"""
                        UPDATE `{tasks_table}`
                        SET state='lost', note=%s, finished_at=%s
                        WHERE host_name=%s AND state='running'
                        """,
                        ("daemon_restart", now, self._node_id),
                    )
                except Exception:
                    pass
        self.conn.execute(
            "UPDATE tasks SET state='lost', note='daemon_restart', finished_at=? WHERE state='running'",
            (now,),
        )
        self.conn.commit()

    def add_running(self, task: RunningTask) -> int:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    row_id, _ = self._mysql_exec(
                        f"""
                        INSERT INTO `{tasks_table}`(
                          host_name, root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            self._node_id,
                            task.root,
                            task.task_key,
                            task.task_type,
                            task.entity_id,
                            task.command_str,
                            task.pid,
                            task.timeout_sec,
                            task.attempts,
                            task.manual_request_id,
                            "running",
                            task.started_at,
                        ),
                    )
                    # Minimal failover shadow (running rows only).
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO tasks(
                          id, root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(row_id),
                            task.root,
                            task.task_key,
                            task.task_type,
                            task.entity_id,
                            task.command_str,
                            task.pid,
                            task.timeout_sec,
                            task.attempts,
                            task.manual_request_id,
                            "running",
                            task.started_at,
                        ),
                    )
                    self.conn.commit()
                    return int(row_id)
                except Exception:
                    pass

        cur = self.conn.execute(
            """
            INSERT INTO tasks(
              root, task_key, task_type, entity_id, command_str, pid, timeout_sec, attempts, manual_request_id, state, started_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task.root,
                task.task_key,
                task.task_type,
                task.entity_id,
                task.command_str,
                task.pid,
                task.timeout_sec,
                task.attempts,
                task.manual_request_id,
                "running",
                task.started_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish(self, row_id: int, state: str, rc: int | None, note: str | None) -> None:
        now = time.time()
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    _, changed = self._mysql_exec(
                        f"""
                        UPDATE `{tasks_table}`
                        SET state=%s, rc=%s, note=%s, finished_at=%s
                        WHERE id=%s AND host_name=%s
                        """,
                        (state, rc, note, now, int(row_id), self._node_id),
                    )
                    if int(changed) > 0:
                        # Keep SQLite tiny in mysql mode: running failover only.
                        self.conn.execute("DELETE FROM tasks WHERE id=?", (int(row_id),))
                        self.conn.commit()
                        return
                except Exception:
                    pass

        self.conn.execute(
            "UPDATE tasks SET state=?, rc=?, note=?, finished_at=? WHERE id=?",
            (state, rc, note, now, int(row_id)),
        )
        self.conn.commit()

    def running_rows(self) -> list[dict[str, object]]:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    return self._mysql_query(
                        f"SELECT * FROM `{tasks_table}` WHERE host_name=%s AND state='running' ORDER BY id ASC",
                        (self._node_id,),
                    )
                except Exception:
                    pass
        return self._sqlite_fetchall_dicts("SELECT * FROM tasks WHERE state='running' ORDER BY id ASC")

    def running_count(self) -> int:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"SELECT COUNT(*) AS cnt FROM `{tasks_table}` WHERE host_name=%s AND state='running'",
                        (self._node_id,),
                    )
                    return int((rows[0].get("cnt") if rows else 0) or 0)
                except Exception:
                    pass
        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE state='running'").fetchone()
        return int(row["cnt"] if row else 0)

    def running_task_summaries(self) -> list[dict[str, object]]:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    return self._mysql_query(
                        f"""
                        SELECT id, root, task_type, entity_id, pid, command_str
                        FROM `{tasks_table}`
                        WHERE host_name=%s AND state='running'
                        ORDER BY id ASC
                        """,
                        (self._node_id,),
                    )
                except Exception:
                    pass
        return self._sqlite_fetchall_dicts(
            "SELECT id, root, task_type, entity_id, pid, command_str FROM tasks WHERE state='running' ORDER BY id ASC"
        )

    def has_running_task_key(self, task_key: str) -> bool:
        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"""
                        SELECT 1 AS present
                        FROM `{tasks_table}`
                        WHERE host_name=%s AND state='running' AND task_key=%s
                        LIMIT 1
                        """,
                        (self._node_id, str(task_key)),
                    )
                    return bool(rows)
                except Exception:
                    pass
        row = self.conn.execute(
            "SELECT 1 AS present FROM tasks WHERE state='running' AND task_key=? LIMIT 1",
            (str(task_key),),
        ).fetchone()
        return bool(row)

    def get_weights(self, kind: str, root: str, max_age_sec: int) -> dict[int, float]:
        min_ts = time.time() - max(1, max_age_sec)
        out: dict[int, float] = {}
        if self._mysql_mode:
            table = self._mysql_tables.get("weight_cache", "")
            if table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"""
                        SELECT entity_id, weight
                        FROM `{table}`
                        WHERE host_name=%s AND kind=%s AND root=%s AND computed_at>=%s
                        """,
                        (self._node_id, kind, root, min_ts),
                    )
                    for row in rows:
                        out[int(row["entity_id"])] = float(row["weight"])
                    return out
                except Exception:
                    pass

        cur = self.conn.execute(
            "SELECT entity_id, weight FROM weight_cache WHERE kind=? AND root=? AND computed_at>=?",
            (kind, root, min_ts),
        )
        for row in cur.fetchall():
            out[int(row["entity_id"])] = float(row["weight"])
        return out

    def put_weights(self, kind: str, root: str, weights: dict[int, float]) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("weight_cache", "")
            if table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"DELETE FROM `{table}` WHERE host_name=%s AND kind=%s AND root=%s",
                        (self._node_id, kind, root),
                    )
                    params = [(self._node_id, kind, root, int(eid), float(w), now) for eid, w in weights.items()]
                    if params:
                        self._mysql_exec_many(
                            f"""
                            INSERT INTO `{table}`(host_name, kind, root, entity_id, weight, computed_at)
                            VALUES(%s,%s,%s,%s,%s,%s)
                            """,
                            params,
                        )
                    # Not required in mysql mode; keep sqlite cache small.
                    self.conn.execute("DELETE FROM weight_cache WHERE kind=? AND root=?", (kind, root))
                    self.conn.commit()
                    return
                except Exception:
                    pass

        self.conn.execute("DELETE FROM weight_cache WHERE kind=? AND root=?", (kind, root))
        self.conn.executemany(
            "INSERT INTO weight_cache(kind, root, entity_id, weight, computed_at) VALUES(?,?,?,?,?)",
            [(kind, root, int(eid), float(w), now) for eid, w in weights.items()],
        )
        self.conn.commit()

    def compact_tasks(
        self,
        *,
        now_ts: float,
        keep_days: int,
        max_rows: int,
        run_vacuum: bool,
    ) -> tuple[int, int, bool]:
        """Compact historical task rows (non-running only).

        Returns: (deleted_rows, remaining_non_running_rows, vacuum_done)
        """
        deleted_rows = 0
        keep_days = max(0, int(keep_days))
        max_rows = max(0, int(max_rows))

        if self._mysql_mode:
            tasks_table = self._mysql_tables.get("tasks", "")
            if tasks_table and self._mysql_available():
                try:
                    if keep_days > 0:
                        cutoff = float(now_ts) - (float(keep_days) * 86400.0)
                        _, cnt = self._mysql_exec(
                            f"""
                            DELETE FROM `{tasks_table}`
                            WHERE host_name=%s AND state!='running' AND COALESCE(finished_at, started_at) < %s
                            """,
                            (self._node_id, cutoff),
                        )
                        deleted_rows += int(cnt)

                    rows = self._mysql_query(
                        f"SELECT COUNT(*) AS cnt FROM `{tasks_table}` WHERE host_name=%s AND state!='running'",
                        (self._node_id,),
                    )
                    non_running = int((rows[0].get("cnt") if rows else 0) or 0)
                    if max_rows > 0 and non_running > max_rows:
                        overflow = non_running - max_rows
                        _, cnt = self._mysql_exec(
                            f"""
                            DELETE FROM `{tasks_table}`
                            WHERE id IN (
                              SELECT id FROM (
                                SELECT id
                                FROM `{tasks_table}`
                                WHERE host_name=%s AND state!='running'
                                ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                                LIMIT %s
                              ) t
                            )
                            """,
                            (self._node_id, overflow),
                        )
                        deleted_rows += int(cnt)
                        rows = self._mysql_query(
                            f"SELECT COUNT(*) AS cnt FROM `{tasks_table}` WHERE host_name=%s AND state!='running'",
                            (self._node_id,),
                        )
                        non_running = int((rows[0].get("cnt") if rows else 0) or 0)

                    # Keep sqlite fallback minimal (running only) in mysql mode.
                    self.conn.execute("DELETE FROM tasks WHERE state!='running'")
                    self.conn.commit()
                    return deleted_rows, non_running, False
                except Exception:
                    pass

        if keep_days > 0:
            cutoff = float(now_ts) - (float(keep_days) * 86400.0)
            cur = self.conn.execute(
                "DELETE FROM tasks WHERE state!='running' AND COALESCE(finished_at, started_at) < ?",
                (cutoff,),
            )
            deleted_rows += int(cur.rowcount or 0)

        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE state!='running'").fetchone()
        non_running = int(row["cnt"] if row else 0)
        if max_rows > 0 and non_running > max_rows:
            overflow = non_running - max_rows
            cur = self.conn.execute(
                """
                DELETE FROM tasks
                WHERE id IN (
                  SELECT id
                  FROM tasks
                  WHERE state!='running'
                  ORDER BY COALESCE(finished_at, started_at) ASC, id ASC
                  LIMIT ?
                )
                """,
                (overflow,),
            )
            deleted_rows += int(cur.rowcount or 0)
            row = self.conn.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE state!='running'").fetchone()
            non_running = int(row["cnt"] if row else 0)

        self.conn.commit()

        vacuum_done = False
        if run_vacuum and deleted_rows > 0:
            self.conn.execute("VACUUM")
            vacuum_done = True

        return deleted_rows, non_running, vacuum_done

    def put_runtime_sync(self, key: str, payload: dict[str, object]) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("runtime_sync", "")
            if table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"""
                        INSERT INTO `{table}`(host_name, `key`, payload_json, updated_at)
                        VALUES(%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE payload_json=VALUES(payload_json), updated_at=VALUES(updated_at)
                        """,
                        (self._node_id, str(key), json.dumps(payload, ensure_ascii=False), now),
                    )
                except Exception:
                    pass
        self.conn.execute(
            """
            INSERT INTO runtime_sync(key, payload_json, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """,
            (str(key), json.dumps(payload, ensure_ascii=False), now),
        )
        self.conn.commit()

    def get_runtime_sync(self, key: str) -> dict[str, object] | None:
        if self._mysql_mode:
            table = self._mysql_tables.get("runtime_sync", "")
            if table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"SELECT payload_json FROM `{table}` WHERE host_name=%s AND `key`=%s LIMIT 1",
                        (self._node_id, str(key)),
                    )
                    if rows:
                        raw = str(rows[0].get("payload_json") or "").strip()
                        if raw:
                            payload = json.loads(raw)
                            if isinstance(payload, dict):
                                return payload
                except Exception:
                    pass
        row = self.conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key=? LIMIT 1",
            (str(key),),
        ).fetchone()
        if not row:
            return None
        raw = str(row["payload_json"] or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def enqueue_manual_request(
        self,
        *,
        root: str,
        task_type: str,
        entity_id: int | None,
        command_str: str,
        timeout_sec: int,
    ) -> int:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    req_id, _ = self._mysql_exec(
                        f"""
                        INSERT INTO `{table}`(
                          host_name, root, task_type, entity_id, command_str, timeout_sec, status, requested_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,'pending',%s)
                        """,
                        (self._node_id, str(root), str(task_type), entity_id, str(command_str), int(timeout_sec), now),
                    )
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO manual_requests(
                          id, root, task_type, entity_id, command_str, timeout_sec, status, requested_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (int(req_id), str(root), str(task_type), entity_id, str(command_str), int(timeout_sec), "pending", now),
                    )
                    self.conn.commit()
                    return int(req_id)
                except Exception:
                    pass

        cur = self.conn.execute(
            """
            INSERT INTO manual_requests(
              root, task_type, entity_id, command_str, timeout_sec, status, requested_at
            ) VALUES(?,?,?,?,?,'pending',?)
            """,
            (
                str(root),
                str(task_type),
                entity_id,
                str(command_str),
                int(timeout_sec),
                now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def pending_manual_requests(self, root: str, limit: int = 32) -> list[dict[str, object]]:
        lim = max(1, int(limit))
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    return self._mysql_query(
                        f"""
                        SELECT *
                        FROM `{table}`
                        WHERE host_name=%s AND status='pending' AND root=%s
                        ORDER BY requested_at ASC, id ASC
                        LIMIT {lim}
                        """,
                        (self._node_id, str(root)),
                    )
                except Exception:
                    pass

        return self._sqlite_fetchall_dicts(
            """
            SELECT *
            FROM manual_requests
            WHERE status='pending' AND root=?
            ORDER BY requested_at ASC, id ASC
            LIMIT ?
            """,
            (str(root), lim),
        )

    def get_manual_request_status(self, req_id: int) -> str | None:
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    rows = self._mysql_query(
                        f"SELECT status FROM `{table}` WHERE id=%s AND host_name=%s LIMIT 1",
                        (int(req_id), self._node_id),
                    )
                    if rows:
                        return str(rows[0].get("status") or "")
                except Exception:
                    pass
        row = self.conn.execute("SELECT status FROM manual_requests WHERE id=?", (int(req_id),)).fetchone()
        if not row:
            return None
        return str(row["status"])

    def cancel_manual_request(self, req_id: int, note: str = "fallback_direct_exec") -> bool:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    _, cnt = self._mysql_exec(
                        f"""
                        UPDATE `{table}`
                        SET status='cancelled', note=%s, finished_at=%s
                        WHERE id=%s AND host_name=%s AND status='pending'
                        """,
                        (str(note), now, int(req_id), self._node_id),
                    )
                    self.conn.execute(
                        """
                        UPDATE manual_requests
                        SET status='cancelled', note=?, finished_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (str(note), now, int(req_id)),
                    )
                    self.conn.commit()
                    return int(cnt) > 0
                except Exception:
                    pass

        cur = self.conn.execute(
            """
            UPDATE manual_requests
            SET status='cancelled', note=?, finished_at=?
            WHERE id=? AND status='pending'
            """,
            (str(note), now, int(req_id)),
        )
        self.conn.commit()
        return int(cur.rowcount or 0) > 0

    def mark_manual_request_launched(self, req_id: int, task_key: str) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"""
                        UPDATE `{table}`
                        SET status='launched', task_key=%s, launched_at=%s, note=NULL
                        WHERE id=%s AND host_name=%s AND status='pending'
                        """,
                        (str(task_key), now, int(req_id), self._node_id),
                    )
                    self.conn.execute(
                        """
                        UPDATE manual_requests
                        SET status='launched', task_key=?, launched_at=?, note=NULL
                        WHERE id=? AND status='pending'
                        """,
                        (str(task_key), now, int(req_id)),
                    )
                    self.conn.commit()
                    return
                except Exception:
                    pass

        self.conn.execute(
            """
            UPDATE manual_requests
            SET status='launched', task_key=?, launched_at=?, note=NULL
            WHERE id=? AND status='pending'
            """,
            (str(task_key), now, int(req_id)),
        )
        self.conn.commit()

    def finish_manual_request(self, req_id: int, status: str, note: str | None = None) -> None:
        now = time.time()
        if self._mysql_mode:
            table = self._mysql_tables.get("manual_requests", "")
            if table and self._mysql_available():
                try:
                    self._mysql_exec(
                        f"""
                        UPDATE `{table}`
                        SET status=%s, note=%s, finished_at=%s
                        WHERE id=%s AND host_name=%s AND status IN ('pending','launched')
                        """,
                        (str(status), note, now, int(req_id), self._node_id),
                    )
                    self.conn.execute(
                        """
                        UPDATE manual_requests
                        SET status=?, note=?, finished_at=?
                        WHERE id=? AND status IN ('pending','launched')
                        """,
                        (str(status), note, now, int(req_id)),
                    )
                    self.conn.commit()
                    return
                except Exception:
                    pass

        self.conn.execute(
            """
            UPDATE manual_requests
            SET status=?, note=?, finished_at=?
            WHERE id=? AND status IN ('pending','launched')
            """,
            (str(status), note, now, int(req_id)),
        )
        self.conn.commit()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_cmdline_args(pid: int) -> list[str]:
    if pid <= 0:
        return []
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return []
    if not raw:
        return []
    args: list[str] = []
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        try:
            args.append(chunk.decode("utf-8", errors="replace"))
        except Exception:
            continue
    return args


def _task_signature_tokens(command_str: str) -> list[str]:
    args = [x for x in str(command_str or "").split(_CMD_SEP) if x]
    signature = [x for x in args if x.startswith("mautic:") or x.endswith("/bin/console") or x == "bin/console"]
    id_opt_names = {"-i", "--id", "--list-id", "--campaign-id", "--segment-id"}
    id_opt_prefixes = ("--id=", "--list-id=", "--campaign-id=", "--segment-id=")
    identity: list[str] = []
    for idx, arg in enumerate(args):
        if arg in id_opt_names and idx + 1 < len(args):
            identity.extend([arg, args[idx + 1]])
            continue
        if any(arg.startswith(prefix) for prefix in id_opt_prefixes):
            identity.append(arg)
    if identity:
        signature.extend(identity)
    if signature:
        return signature
    # Fallback for unexpected/custom templates: use short tail.
    return args[-2:] if len(args) >= 2 else args


def _cmdline_has_token(cmdline_args: list[str], token: str) -> bool:
    if token in cmdline_args:
        return True
    if "/" in token:
        token_base = os.path.basename(token.rstrip("/"))
        if not token_base:
            return False
        for arg in cmdline_args:
            if os.path.basename(str(arg).rstrip("/")) == token_base:
                return True
    return False


def _pid_matches_task_command(pid: int, command_str: str) -> bool:
    cmdline_args = _pid_cmdline_args(pid)
    if not cmdline_args:
        return False
    signature = _task_signature_tokens(command_str)
    if not signature:
        return False
    return all(_cmdline_has_token(cmdline_args, tok) for tok in signature)


def _kill_pid(pid: int, grace_sec: int) -> None:
    if not _is_pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + max(0, grace_sec)
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return
        time.sleep(0.5)
    if _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _spawn_command(*, root: str, args: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        args,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _task_key(root: str, task_type: str, entity_id: int | None) -> str:
    eid = "-" if entity_id is None else str(entity_id)
    return f"{root}|{task_type}|{eid}"


def _task_repeat_interval_sec(config: AgentConfig, task_type: str) -> int:
    if task_type == "segment_sql":
        return max(0, int(getattr(config, "segment_sql_min_repeat_sec", 0)))
    if task_type == "campaign_trigger":
        return max(0, int(getattr(config, "campaign_trigger_min_repeat_sec", 0)))
    if task_type in {"campaign_rebuild", "campaign_update"}:
        return max(0, int(getattr(config, "campaign_rebuild_min_repeat_sec", 0)))
    return 0


def _entity_launch_guard_key(root: str, task_type: str, entity_id: int | None) -> str:
    return f"{_task_key(root, task_type, entity_id)}|launch"


def _launch_allowed(config: AgentConfig, root: str, task_type: str, entity_id: int | None, now_ts: float | None = None) -> bool:
    min_repeat = _task_repeat_interval_sec(config, task_type)
    if min_repeat <= 0 or entity_id is None:
        return True
    ts = float(now_ts if now_ts is not None else time.time())
    key = _entity_launch_guard_key(root, task_type, entity_id)
    prev = _ENTITY_LAUNCH_GUARD.get(key, 0.0)
    return (ts - prev) >= float(min_repeat)


def _prune_entity_launch_guard(max_age_sec: int = 3600, now_ts: float | None = None) -> None:
    if not _ENTITY_LAUNCH_GUARD:
        return
    ts = float(now_ts if now_ts is not None else time.time())
    stale = [k for k, v in _ENTITY_LAUNCH_GUARD.items() if ts - float(v) > float(max_age_sec)]
    for k in stale:
        _ENTITY_LAUNCH_GUARD.pop(k, None)


def _running_count(running: dict[str, RunningTask], root: str, task_type_prefix: str) -> int:
    return sum(1 for t in running.values() if t.root == root and t.task_type.startswith(task_type_prefix))


def _running_campaign_total(running: dict[str, RunningTask], root: str) -> int:
    return sum(
        1
        for t in running.values()
        if t.root == root and t.task_type in {"campaign_update", "campaign_trigger", "campaign_rebuild"}
    )


def _running_count_for_entities(
    running: dict[str, RunningTask],
    root: str,
    task_type: str,
    entity_ids: set[int] | None,
) -> int:
    if entity_ids is None:
        return _running_count(running, root, task_type)
    return sum(
        1
        for t in running.values()
        if t.root == root and t.task_type == task_type and t.entity_id is not None and t.entity_id in entity_ids
    )


def _is_running(running: dict[str, RunningTask], root: str, task_type: str, entity_id: int | None) -> bool:
    for t in running.values():
        if t.root == root and t.task_type == task_type and t.entity_id == entity_id:
            return True
    return False


def _ring_has_launchable(
    ring: deque[int],
    *,
    running: dict[str, RunningTask],
    root: str,
    task_type: str,
) -> bool:
    for eid in ring:
        if not _is_running(running, root, task_type, eid):
            return True
    return False


def _running_entities_stuck_for(
    running: dict[str, RunningTask],
    *,
    root: str,
    task_type: str,
    entity_ids: set[int],
    now_ts: float,
    stuck_sec: int,
) -> int:
    if stuck_sec <= 0:
        return 0
    count = 0
    for t in running.values():
        if t.root != root or t.task_type != task_type or t.entity_id is None:
            continue
        if t.entity_id not in entity_ids:
            continue
        if now_ts - float(t.started_at) >= float(stuck_sec):
            count += 1
    return count


def _submit_if_slot(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    root: str,
    task_type: str,
    entity_id: int | None,
    args: list[str],
    timeout_sec: int,
    max_parallel_for_type: int,
    popens: dict[str, subprocess.Popen[bytes]] | None = None,
    ignore_limit: bool = False,
    manual_request_id: int | None = None,
) -> bool:
    key = _task_key(root, task_type, entity_id)
    if key in running:
        return False
    if store.has_running_task_key(key):
        return False
    if manual_request_id is None and not _launch_allowed(config, root, task_type, entity_id):
        return False
    if not ignore_limit and _running_count(running, root, task_type) >= max_parallel_for_type:
        return False

    proc = _spawn_command(root=root, args=args)
    task = RunningTask(
        row_id=0,
        root=root,
        task_key=key,
        task_type=task_type,
        entity_id=entity_id,
        command_str=_CMD_SEP.join(args),
        timeout_sec=timeout_sec,
        attempts=1,
        started_at=time.time(),
        pid=proc.pid,
        manual_request_id=manual_request_id,
    )
    task.row_id = store.add_running(task)
    running[key] = task
    if popens is not None:
        popens[key] = proc
    _ENTITY_LAUNCH_GUARD[_entity_launch_guard_key(root, task_type, entity_id)] = task.started_at
    logging.info("[%s] spawned %s pid=%s entity=%s", root, task_type, task.pid, entity_id)
    return True


def _respawn_task(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    task: RunningTask,
) -> bool:
    # task_retry_max semantics:
    # - <= 0 : unlimited retries
    # - 1    : no retry (initial run only)
    # - > 1  : bounded retries up to configured attempt cap
    retry_max = int(config.task_retry_max)
    if retry_max > 0 and task.attempts >= retry_max:
        return False
    try:
        args = task.command_str.split(_CMD_SEP)
        proc = _spawn_command(root=task.root, args=args)
    except Exception as e:  # pragma: no cover - defensive
        logging.warning("[%s] respawn failed %s: %s", task.root, task.task_type, e)
        return False

    next_task = RunningTask(
        row_id=0,
        root=task.root,
        task_key=task.task_key,
        task_type=task.task_type,
        entity_id=task.entity_id,
        command_str=task.command_str,
        timeout_sec=task.timeout_sec,
        attempts=task.attempts + 1,
        started_at=time.time(),
        pid=proc.pid,
        manual_request_id=task.manual_request_id,
    )
    next_task.row_id = store.add_running(next_task)
    running[next_task.task_key] = next_task
    popens[next_task.task_key] = proc
    logging.warning(
        "[%s] retry %s entity=%s attempt=%s pid=%s",
        task.root,
        task.task_type,
        task.entity_id,
        next_task.attempts,
        next_task.pid,
    )
    return True


def _monitor_running(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
) -> None:
    now = time.time()
    for key, task in list(running.items()):
        proc = popens.get(key)
        if proc is not None:
            rc = proc.poll()
            if rc is not None:
                state = "done" if rc == 0 else "failed"
                note = None if rc == 0 else "non_zero_exit"
                store.finish(task.row_id, state=state, rc=rc, note=note)
                running.pop(key, None)
                popens.pop(key, None)
                respawned = False
                if rc != 0:
                    logging.warning("[%s] %s entity=%s failed rc=%s", task.root, task.task_type, task.entity_id, rc)
                    retry_enabled = (config.task_retry_max <= 0) or (config.task_retry_max > 1)
                    if retry_enabled:
                        if config.task_retry_delay_sec > 0:
                            time.sleep(config.task_retry_delay_sec)
                        respawned = _respawn_task(config=config, store=store, running=running, popens=popens, task=task)
                if task.manual_request_id is not None and not respawned:
                    store.finish_manual_request(task.manual_request_id, state, note)
                continue

        alive = _is_pid_alive(task.pid)
        if alive and proc is None and not _pid_matches_task_command(task.pid, task.command_str):
            store.finish(task.row_id, state="lost", rc=None, note="pid_cmd_mismatch")
            running.pop(key, None)
            popens.pop(key, None)
            if task.manual_request_id is not None:
                store.finish_manual_request(task.manual_request_id, "lost", "pid_cmd_mismatch")
            logging.warning(
                "[%s] %s entity=%s pid=%s command-mismatch detected, marking lost",
                task.root,
                task.task_type,
                task.entity_id,
                task.pid,
            )
            continue

        elapsed = now - task.started_at
        timeout_threshold: int | None = None
        if task.timeout_sec > 0 and config.worker_watchdog_sec > 0:
            timeout_threshold = min(task.timeout_sec, config.worker_watchdog_sec)
        elif task.timeout_sec > 0:
            timeout_threshold = task.timeout_sec
        elif config.worker_watchdog_sec > 0:
            timeout_threshold = config.worker_watchdog_sec

        if alive and (timeout_threshold is None or elapsed <= timeout_threshold):
            continue

        if alive and timeout_threshold is not None and elapsed > timeout_threshold:
            _kill_pid(task.pid, config.segment_kill_grace_sec)
            store.finish(task.row_id, state="timeout", rc=None, note="killed_by_timeout")
            running.pop(key, None)
            popens.pop(key, None)
            logging.warning("[%s] %s entity=%s timeout=%ss", task.root, task.task_type, task.entity_id, int(timeout_threshold))
            allow_restart = (config.worker_stuck_policy or "skip").lower() == "restart"
            restart_cap = max(0, config.worker_stuck_restart_limit)
            respawned = False
            if allow_restart and task.attempts <= restart_cap:
                if config.task_retry_delay_sec > 0:
                    time.sleep(config.task_retry_delay_sec)
                respawned = _respawn_task(config=config, store=store, running=running, popens=popens, task=task)
            if task.manual_request_id is not None and not respawned:
                store.finish_manual_request(task.manual_request_id, "timeout", "killed_by_timeout")
            continue

        if not alive:
            store.finish(task.row_id, state="lost", rc=None, note="pid_not_alive")
            running.pop(key, None)
            popens.pop(key, None)
            if task.manual_request_id is not None:
                store.finish_manual_request(task.manual_request_id, "lost", "pid_not_alive")
            logging.warning("[%s] %s entity=%s pid_lost=%s", task.root, task.task_type, task.entity_id, task.pid)


def _running_task_from_row(row: dict[str, object]) -> RunningTask:
    return RunningTask(
        row_id=int(row["id"]),
        root=str(row["root"]),
        task_key=str(row["task_key"]),
        task_type=str(row["task_type"]),
        entity_id=int(row["entity_id"]) if row["entity_id"] is not None else None,
        command_str=str(row["command_str"]),
        timeout_sec=int(row["timeout_sec"]),
        attempts=int(row["attempts"]),
        started_at=float(row["started_at"]),
        pid=int(row["pid"]),
        manual_request_id=int(row["manual_request_id"]) if row["manual_request_id"] is not None else None,
    )


def _reconcile_running_state(
    *,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
) -> dict[str, int]:
    stats = {
        "tracked_total": 0,
        "kept": 0,
        "adopted": 0,
        "lost_pid": 0,
        "lost_cmd": 0,
        "duplicate": 0,
    }
    rows = store.running_rows()
    stats["tracked_total"] = len(rows)
    if not rows:
        return stats

    valid_by_key: dict[str, RunningTask] = {}
    for row in rows:
        task = _running_task_from_row(row)
        if not _is_pid_alive(task.pid):
            store.finish(task.row_id, state="lost", rc=None, note="pid_not_alive")
            stats["lost_pid"] += 1
            continue
        if not _pid_matches_task_command(task.pid, task.command_str):
            store.finish(task.row_id, state="lost", rc=None, note="pid_cmd_mismatch")
            stats["lost_cmd"] += 1
            continue

        prev = valid_by_key.get(task.task_key)
        if prev is None:
            valid_by_key[task.task_key] = task
            continue

        keep = prev
        current = running.get(task.task_key)
        if current is not None:
            if task.pid == current.pid:
                keep = task
            elif prev.pid == current.pid:
                keep = prev
            elif (task.started_at, task.row_id) >= (prev.started_at, prev.row_id):
                keep = task
        elif (task.started_at, task.row_id) >= (prev.started_at, prev.row_id):
            keep = task

        loser = prev if keep is task else task
        store.finish(loser.row_id, state="lost", rc=None, note="duplicate_task_key")
        stats["duplicate"] += 1
        valid_by_key[task.task_key] = keep

    for key, task in valid_by_key.items():
        cur = running.get(key)
        if cur is None:
            running[key] = task
            proc = popens.get(key)
            if proc is not None and proc.pid != task.pid:
                popens.pop(key, None)
            stats["adopted"] += 1
            continue
        if (
            cur.row_id != task.row_id
            or cur.pid != task.pid
            or cur.attempts != task.attempts
            or abs(cur.started_at - task.started_at) > 0.001
        ):
            running[key] = task
            proc = popens.get(key)
            if proc is not None and proc.pid != task.pid:
                popens.pop(key, None)
            stats["adopted"] += 1
        else:
            stats["kept"] += 1
    return stats


def _load_orphans_from_store(store: TaskStore) -> dict[str, RunningTask]:
    out: dict[str, RunningTask] = {}
    for row in store.running_rows():
        t = _running_task_from_row(row)
        out[t.task_key] = t
    return out


def _dispatch_manual_requests_for_root(
    *,
    config: AgentConfig,
    store: TaskStore,
    running: dict[str, RunningTask],
    popens: dict[str, subprocess.Popen[bytes]],
    root: str,
    seg_sql_ring: deque[int],
    seg_prio_ring: deque[int],
    seg_reg_ring: deque[int],
    trg_prio_ring: deque[int],
    trg_reg_ring: deque[int],
    reb_prio_ring: deque[int],
    reb_reg_ring: deque[int],
) -> None:
    pending = store.pending_manual_requests(root, limit=64)
    if not pending:
        return
    for row in pending:
        req_id = int(row["id"])
        task_type = str(row["task_type"] or "").strip()
        entity_id = int(row["entity_id"]) if row["entity_id"] is not None else None
        raw_cmd = str(row["command_str"] or "").strip()
        timeout_sec = int(row["timeout_sec"] or config.command_timeout_sec)
        if not task_type or not raw_cmd:
            store.finish_manual_request(req_id, "failed", "invalid_request_payload")
            continue
        args = raw_cmd.split(_CMD_SEP)
        if not args:
            store.finish_manual_request(req_id, "failed", "empty_command")
            continue
        if task_type == "segment" and entity_id is not None:
            seg_sql_state = _segment_sql_state_load(store, root, entity_id)
            if str(seg_sql_state.get("status") or "").strip().lower() == "running":
                store.finish_manual_request(req_id, "skipped", "managed_by_segment_sql_running")
                continue

        launched = _submit_if_slot(
            config=config,
            store=store,
            running=running,
            root=root,
            task_type=task_type,
            entity_id=entity_id,
            args=args,
            timeout_sec=timeout_sec,
            max_parallel_for_type=1,
            popens=popens,
            ignore_limit=True,
            manual_request_id=req_id,
        )
        if not launched:
            store.finish_manual_request(req_id, "skipped", "already_running")
            continue

        task_key = _task_key(root, task_type, entity_id)
        store.mark_manual_request_launched(req_id, task_key)

        if task_type == "segment":
            _mark_ring_entity_executed(seg_sql_ring, entity_id)
            _mark_ring_entity_executed(seg_prio_ring, entity_id)
            _mark_ring_entity_executed(seg_reg_ring, entity_id)
        elif task_type == "campaign_trigger":
            _mark_ring_entity_executed(trg_prio_ring, entity_id)
            _mark_ring_entity_executed(trg_reg_ring, entity_id)
        elif task_type == "campaign_rebuild":
            _mark_ring_entity_executed(reb_prio_ring, entity_id)
            _mark_ring_entity_executed(reb_reg_ring, entity_id)

        logging.info(
            "[%s] manual request launched id=%s task=%s entity=%s",
            root,
            req_id,
            task_type,
            entity_id,
        )


def run_loop(config: AgentConfig, single_cycle: bool = False) -> None:
    logging.info("MCD loop started")
    base_config = config

    segment_whitelist = set(config.segment_whitelist) | _load_id_file(config.segment_whitelist_file)
    campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
    if config.disable_whitelist:
        segment_whitelist = set()
        campaign_whitelist = set()

    store = TaskStore(config.state_db_path, config)
    store.put_runtime_sync("local_runtime", local_runtime_overrides(config))
    inventory = InstanceInventory(config.state_db_path)
    seeded = ensure_seeded(inventory, config)
    logging.info("Instance inventory loaded: %s", seeded)
    try:
        rows, source = fetch_custom_manifest(config, use_cache_on_error=True)
        logging.info("Custom scripts manifest loaded: source=%s count=%s", source, len(rows))
    except Exception as e:
        logging.warning("Custom scripts manifest prefetch failed: %s", e)

    running = _load_orphans_from_store(store)
    popens: dict[str, subprocess.Popen[bytes]] = {}

    identity = resolve_agent_identity(config)
    if bool(identity.get("clone_detected", False)):
        try:
            installs = inventory.refresh_from_discovery(config)
            logging.info(
                "template clone detected: source=%s local=%s; inventory rescan applied (%s instances)",
                str(identity.get("source_host_name") or "-"),
                str(identity.get("local_hostname") or "-"),
                len(installs),
            )
        except Exception as e:
            logging.warning("template clone inventory rescan failed; fallback to cached inventory: %s", e)
            installs = inventory.list_instances()
    else:
        installs = inventory.list_instances()
    segment_sql_rings: dict[str, deque[int]] = {}
    segment_sql_rules_by_root: dict[str, dict[int, SQLSegmentRule]] = {}
    segment_sql_active_sets: dict[str, set[int]] = {}
    segment_sql_done_sets: dict[str, set[int]] = {}
    segment_prio_rings: dict[str, deque[int]] = {}
    segment_reg_rings: dict[str, deque[int]] = {}
    campaign_trigger_prio_rings: dict[str, deque[int]] = {}
    campaign_trigger_reg_rings: dict[str, deque[int]] = {}
    campaign_rebuild_prio_rings: dict[str, deque[int]] = {}
    campaign_rebuild_reg_rings: dict[str, deque[int]] = {}
    segment_prio_sets: dict[str, set[int]] = {}
    segment_reg_sets: dict[str, set[int]] = {}
    campaign_trigger_prio_sets: dict[str, set[int]] = {}
    campaign_trigger_reg_sets: dict[str, set[int]] = {}
    campaign_rebuild_prio_sets: dict[str, set[int]] = {}
    campaign_rebuild_reg_sets: dict[str, set[int]] = {}
    campaign_round_robin: dict[str, int] = {}
    segment_resume_rings: dict[str, deque[int]] = {}
    queue_samples: dict[str, deque[tuple[float, int]]] = {}
    throttled: dict[str, bool] = {}
    last_import_poll_ts: dict[str, float] = {}
    import_pending_cache: dict[str, int] = {}
    segment_last_full_scan_ts: dict[str, float] = {}
    segment_force_full_scan_until: dict[str, float] = {}
    last_cleanup_ts: dict[str, float] = {}
    last_cache_clear_ts: dict[str, float] = {}
    last_cache_warm_ts: dict[str, float] = {}
    last_fs_permissions_guard_ts: dict[str, float] = {}
    last_db_watchdog_ts: dict[str, float] = {}
    last_tasks_compact_ts = 0.0
    last_outbound_events_prune_ts = 0.0
    last_custom_cache_cleanup_ts = 0.0
    last_launch_guard_prune_ts = 0.0
    next_scheduler_reconcile_at = 0.0
    db_dispatch_pause_until: dict[str, float] = {}
    db_dispatch_pause_reasons: dict[str, str] = {}
    jobs_last_run: dict[tuple[str, str], float] = {}
    last_backup_schedule_ts = 0.0
    last_backup_schedule_day = ""
    backup_thread: threading.Thread | None = None
    backup_dispatch_pause_active = False
    scheduler_dispatch_pause_active = False
    next_plan_refresh_at = 0.0
    next_update_check_at = 0.0
    update_deferred_by_backup = False
    next_service_profile_apply_at = 0.0
    next_backup_profile_sync_at = 0.0
    next_runtime_overrides_poll_at = 0.0
    runtime_overrides_sync_requested = False
    # Always perform an initial runtime-overrides sync after daemon start/restart.
    # This is required even when periodic polling is disabled to avoid running
    # with stale local-only runtime after self-update/service restart.
    startup_runtime_sync_pending = bool(config.mcc_url and config.mcc_token)
    next_profile_guard_at = 0.0
    last_runtime_overrides_fp = ""
    last_runtime_overrides_error = ""
    last_local_runtime_fp = overrides_fingerprint(local_runtime_overrides(config))
    pusher = MCCStatePusher(config)

    while True:
        _monitor_running(config=config, store=store, running=running, popens=popens)
        now = time.time()
        if now >= next_scheduler_reconcile_at:
            rec = _reconcile_running_state(store=store, running=running, popens=popens)
            if any(int(rec.get(k, 0) or 0) > 0 for k in ("adopted", "lost_pid", "lost_cmd", "duplicate")):
                logging.warning(
                    "scheduler reconcile: tracked=%s kept=%s adopted=%s lost_pid=%s lost_cmd=%s duplicate=%s",
                    int(rec.get("tracked_total", 0) or 0),
                    int(rec.get("kept", 0) or 0),
                    int(rec.get("adopted", 0) or 0),
                    int(rec.get("lost_pid", 0) or 0),
                    int(rec.get("lost_cmd", 0) or 0),
                    int(rec.get("duplicate", 0) or 0),
                )
            next_scheduler_reconcile_at = now + max(15, int(config.scheduler_reconcile_interval_sec or 60))
        if now - last_launch_guard_prune_ts >= 300:
            _prune_entity_launch_guard(max_age_sec=3600, now_ts=now)
            last_launch_guard_prune_ts = now

        local_runtime = local_runtime_overrides(config)
        local_fp = overrides_fingerprint(local_runtime)
        if local_fp != last_local_runtime_fp:
            store.put_runtime_sync("local_runtime", local_runtime)
            last_local_runtime_fp = local_fp
            if config.mcc_url and config.mcc_token:
                pushed = push_runtime_overrides(config, local_runtime, merge=False)
                p_status = str(pushed.get("status", "")).strip().lower()
                if p_status == "ok":
                    logging.info(
                        "runtime-overrides local change pushed to MCC (keys=%s)",
                        ",".join(sorted(local_runtime.keys())) if local_runtime else "-",
                    )
                elif p_status != "disabled":
                    logging.warning("runtime-overrides local push failed: %s", pushed.get("reason", "unknown"))

        if consume_poll_trigger(config):
            runtime_overrides_sync_requested = True
            next_runtime_overrides_poll_at = 0.0
            logging.info("runtime-overrides poll trigger consumed: immediate MCC sync requested")

        runtime_poll_enabled = bool(getattr(config, "mcc_runtime_overrides_poll_enabled", False))
        should_runtime_poll = runtime_poll_enabled and now >= next_runtime_overrides_poll_at
        if config.mcc_url and config.mcc_token and (
            startup_runtime_sync_pending or runtime_overrides_sync_requested or should_runtime_poll
        ):
            ro = fetch_runtime_overrides(config)
            status = str(ro.get("status", "")).strip().lower()
            poll_interval = max(15, min(300, int(config.mcc_push_interval_sec or config.poll_interval_sec or 60)))
            if status == "ok":
                overrides_raw = ro.get("runtime_overrides")
                overrides = overrides_raw if isinstance(overrides_raw, dict) else {}
                store.put_runtime_sync("mcc_runtime", overrides)
                fp = overrides_fingerprint(overrides)
                if fp != last_runtime_overrides_fp:
                    applied = apply_remote_overrides(base_config, overrides)
                    next_cfg = applied["config"]
                    applied_keys = list(applied.get("applied_keys", []))
                    unsupported_keys = list(applied.get("unsupported_keys", []))
                    blocked_keys = list(applied.get("blocked_keys", []))
                    if unsupported_keys:
                        logging.warning(
                            "runtime-overrides ignored unsupported keys: %s",
                            ",".join(str(x) for x in unsupported_keys),
                        )
                    if blocked_keys:
                        logging.warning(
                            "runtime-overrides ignored blocked keys (restart/static-only): %s",
                            ",".join(str(x) for x in blocked_keys),
                        )
                    if next_cfg != config:
                        old_profile = (config.profile_name or "").strip().lower()
                        config = next_cfg
                        pusher.cfg = config
                        next_plan_refresh_at = 0.0
                        next_update_check_at = 0.0
                        next_service_profile_apply_at = 0.0
                        segment_whitelist = set(config.segment_whitelist) | _load_id_file(config.segment_whitelist_file)
                        campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
                        if config.disable_whitelist:
                            segment_whitelist = set()
                            campaign_whitelist = set()
                        new_profile = (config.profile_name or "").strip().lower()
                        logging.info(
                            "runtime-overrides applied from MCC: keys=%s profile=%s->%s",
                            ",".join(applied_keys) if applied_keys else "-",
                            old_profile or "-",
                            new_profile or "-",
                        )
                    else:
                        logging.info(
                            "runtime-overrides synced from MCC: no effective change (keys=%s)",
                            ",".join(applied_keys) if applied_keys else "-",
                        )
                    _persist_stable_backup_runtime_to_config(next_cfg if isinstance(next_cfg, AgentConfig) else config, applied_keys)
                    last_runtime_overrides_fp = fp
                    store.put_runtime_sync(
                        "active_runtime",
                        {
                            "profile": (config.profile_name or "").strip().lower(),
                            "applied_keys": applied_keys,
                            "blocked_keys": blocked_keys,
                            "unsupported_keys": unsupported_keys,
                        },
                    )
                startup_runtime_sync_pending = False
                last_runtime_overrides_error = ""
                next_runtime_overrides_poll_at = now + poll_interval
            elif status == "disabled":
                startup_runtime_sync_pending = False
                last_runtime_overrides_error = ""
                next_runtime_overrides_poll_at = now + poll_interval
            else:
                reason = str(ro.get("reason", "unknown")).strip() or "unknown"
                if reason != last_runtime_overrides_error:
                    logging.warning("runtime-overrides fetch failed: %s", reason)
                last_runtime_overrides_error = reason
                next_runtime_overrides_poll_at = now + max(30, poll_interval)
            runtime_overrides_sync_requested = False

        if now >= next_backup_profile_sync_at:
            try:
                sync_res = backup_profile_sync_from_config(config)
                if bool(sync_res.get("changed")):
                    keys_raw = sync_res.get("keys")
                    keys = keys_raw if isinstance(keys_raw, list) else []
                    logging.info(
                        "backup-profile synced from config to state DB: keys=%s",
                        ",".join(str(x) for x in keys) if keys else "-",
                    )
            except Exception as e:
                logging.warning("backup-profile sync from config failed: %s", e)
            next_backup_profile_sync_at = now + 60.0

        if config.tasks_compact_enabled:
            quiet_hour = max(0, min(23, int(config.tasks_compact_quiet_hour)))
            quiet_window_min = max(1, min(180, int(config.tasks_compact_quiet_window_min)))
            interval_sec = max(300, int(config.tasks_compact_interval_sec))
            dt = datetime.now()
            in_quiet = dt.hour == quiet_hour and dt.minute < quiet_window_min
            if in_quiet and (last_tasks_compact_ts == 0.0 or now - last_tasks_compact_ts >= interval_sec):
                can_vacuum = bool(config.tasks_compact_vacuum and not running)
                try:
                    deleted, remaining, vacuum_done = store.compact_tasks(
                        now_ts=now,
                        keep_days=config.tasks_history_keep_days,
                        max_rows=config.tasks_history_max_rows,
                        run_vacuum=can_vacuum,
                    )
                    if deleted > 0 or vacuum_done:
                        logging.info(
                            "tasks compaction: deleted=%s remaining_non_running=%s vacuum=%s",
                            deleted,
                            remaining,
                            "on" if vacuum_done else "off",
                        )
                    elif config.tasks_compact_vacuum and running:
                        logging.info("tasks compaction: vacuum postponed (running tasks=%s)", len(running))
                except Exception as e:
                    logging.warning("tasks compaction failed: %s", e)
                last_tasks_compact_ts = now

        # Keep outbound profile-event queue bounded; old delivered events do
        # not have operational value after retention window.
        quiet_hour = max(0, min(23, int(config.tasks_compact_quiet_hour)))
        quiet_window_min = max(1, min(180, int(config.tasks_compact_quiet_window_min)))
        interval_sec = max(300, int(config.tasks_compact_interval_sec))
        dt = datetime.now()
        in_quiet = dt.hour == quiet_hour and dt.minute < quiet_window_min
        if in_quiet and (
            last_outbound_events_prune_ts == 0.0
            or now - last_outbound_events_prune_ts >= interval_sec
        ):
            try:
                removed = prune_sent_profile_events(
                    config,
                    keep_days=max(1, int(config.outbound_events_sent_keep_days)),
                )
                if removed > 0:
                    logging.info("outbound events prune: removed=%s", removed)
            except Exception as e:
                logging.warning("outbound events prune failed: %s", e)
            last_outbound_events_prune_ts = now

        if config.custom_cache_cleanup_enabled:
            quiet_hour = max(0, min(23, int(config.custom_cache_cleanup_quiet_hour)))
            quiet_window_min = max(1, min(180, int(config.custom_cache_cleanup_quiet_window_min)))
            interval_sec = max(300, int(config.custom_cache_cleanup_interval_sec))
            dt = datetime.now()
            in_quiet = dt.hour == quiet_hour and dt.minute < quiet_window_min
            if in_quiet and (
                last_custom_cache_cleanup_ts == 0.0
                or now - last_custom_cache_cleanup_ts >= interval_sec
            ):
                try:
                    stats = cleanup_custom_cache(config, known_keys=cached_custom_manifest_keys(config))
                    if int(stats.get("logs_removed", 0) or 0) > 0 or int(stats.get("downloads_removed", 0) or 0) > 0:
                        logging.info(
                            "custom cache cleanup: logs_removed=%s downloads_removed=%s errors=%s",
                            int(stats.get("logs_removed", 0) or 0),
                            int(stats.get("downloads_removed", 0) or 0),
                            int(stats.get("errors", 0) or 0),
                        )
                except Exception as e:
                    logging.warning("custom cache cleanup failed: %s", e)
                last_custom_cache_cleanup_ts = now

        if now >= next_update_check_at:
            update_backup_locked = False
            if config.backup_enabled:
                try:
                    update_backup_locked = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(config)
                except Exception as e:
                    logging.warning("auto-update backup lock check failed: %s", e)
                    update_backup_locked = False

            if update_backup_locked:
                if not update_deferred_by_backup:
                    logging.info("auto-update deferred: backup lock active; retry on next cycle")
                update_deferred_by_backup = True
                next_update_check_at = now + max(5, int(config.poll_interval_sec or 10))
            else:
                if update_deferred_by_backup:
                    logging.info("auto-update defer cleared: backup lock released")
                update_deferred_by_backup = False
                try:
                    note, wait_sec = maybe_auto_update(config)
                    if note:
                        logging.info("%s", note)
                    next_update_check_at = now + max(60, int(wait_sec or config.mcd_update_check_interval_sec))
                except Exception as e:
                    logging.warning("auto-update check failed: %s", e)
                    next_update_check_at = now + max(120, int(config.mcd_update_check_interval_sec))

        if (
            config.service_profiles_enabled
            and config.service_profiles_auto_apply
            and now >= next_service_profile_apply_at
        ):
            try:
                components = [c.strip().lower() for c in (config.service_profiles_components or []) if str(c).strip()]
                if not components:
                    components = ["php_fpm", "mysql"]
                for comp in components:
                    if comp not in {"php_fpm", "php-fpm", "mysql", "apt"}:
                        continue
                    res = service_profiles_apply_once(config, component=comp, dry_run=False)
                    status = str(res.get("status", "")).strip().lower()
                    if status == "ok":
                        applied = res.get("apply")
                        if isinstance(applied, dict):
                            logging.info("service-profile %s apply: %s", comp, applied.get("status", "ok"))
                    elif status == "skipped":
                        logging.info("service-profile %s skipped: %s", comp, res.get("reason", "-"))
                    else:
                        logging.warning("service-profile %s apply failed: %s", comp, res.get("reason", "unknown"))
                next_service_profile_apply_at = now + max(300, int(config.service_profiles_poll_interval_sec or 3600))
            except Exception as e:
                logging.warning("service-profile auto-apply failed: %s", e)
                next_service_profile_apply_at = now + max(300, int(config.service_profiles_poll_interval_sec or 3600))

        if config.backup_enabled and config.backup_schedule_enabled:
            quiet_hour = max(0, min(23, int(config.backup_schedule_quiet_hour)))
            quiet_window_min = max(1, min(180, int(config.backup_schedule_quiet_window_min)))
            interval_sec = max(300, int(config.backup_schedule_interval_sec))
            dt_local = datetime.now()
            in_quiet = dt_local.hour == quiet_hour and dt_local.minute < quiet_window_min
            if in_quiet and (last_backup_schedule_ts == 0.0 or now - last_backup_schedule_ts >= interval_sec):
                run_day = dt_local.strftime("%Y-%m-%d")
                if run_day == last_backup_schedule_day:
                    # Already executed (or skipped by completed state) for current daily slot.
                    pass
                elif backup_thread is not None and backup_thread.is_alive():
                    logging.info("backup schedule: previous run still active, skip this slot")
                elif _backup_done_for_local_date(config, dt_local):
                    logging.info("backup schedule: successful backup for %s already exists, skip", run_day)
                    last_backup_schedule_day = run_day
                    last_backup_schedule_ts = now
                else:
                    def _backup_worker() -> None:
                        try:
                            res = backup_run(config, None)
                            if res.ok:
                                logging.info("backup schedule: %s", res.message)
                            else:
                                logging.warning("backup schedule failed: %s", res.message)
                        except Exception as e:
                            logging.warning("backup schedule failed: %s", e)
                    backup_thread = threading.Thread(target=_backup_worker, name="mcd-backup", daemon=True)
                    backup_thread.start()
                    last_backup_schedule_ts = now
                    last_backup_schedule_day = run_day

        if pusher.enabled() and should_poll_alert(now, pusher.last_alert_poll_ts, config.mcc_push_alert_poll_interval_sec):
            try:
                signals_payload = collect_signals(window_min=config.mcc_push_alert_window_min, cfg=config)
                pusher.set_signals(signals_payload, now)
            except Exception as e:
                logging.warning("signals collect failed: %s", e)
            pusher.last_alert_poll_ts = now

        if config.host_pressure_pause_enabled and installs:
            sig = pusher.latest_signals if isinstance(pusher.latest_signals, dict) else {}
            totals = sig.get("totals") if isinstance(sig.get("totals"), dict) else {}
            php_stuck = int(totals.get("php_console_stuck", 0) or 0) if isinstance(totals, dict) else 0
            swap_level = int(totals.get("swap_pressure_level", 0) or 0) if isinstance(totals, dict) else 0
            pressure_reasons: list[str] = []
            if (
                config.host_pressure_php_stuck_pause_threshold > 0
                and php_stuck >= int(config.host_pressure_php_stuck_pause_threshold or 0)
            ):
                pressure_reasons.append(f"php_console_stuck={php_stuck}")
            if (
                config.host_pressure_swap_level_pause_threshold > 0
                and swap_level >= int(config.host_pressure_swap_level_pause_threshold or 0)
            ):
                pressure_reasons.append(f"swap_pressure_level={swap_level}")
            if pressure_reasons:
                pause_sec = int(
                    effective_db_watchdog_config(config).get("dispatch_pause_sec", _DB_DISPATCH_PAUSE_SEC)
                    or _DB_DISPATCH_PAUSE_SEC
                )
                reason = "host pressure: " + ", ".join(pressure_reasons)
                for inst in installs:
                    root = str(getattr(inst, "root", "") or "").strip()
                    if not root:
                        continue
                    _mark_db_dispatch_pause(
                        root=root,
                        reason=reason,
                        now_ts=now,
                        pause_until=db_dispatch_pause_until,
                        pause_reasons=db_dispatch_pause_reasons,
                        pause_sec=pause_sec,
                    )

        if now >= next_plan_refresh_at:
            installs = inventory.list_instances()
            logging.info("Instance inventory count: %d", len(installs))
            now_utc = datetime.now(timezone.utc)
            now_ts = int(now_utc.timestamp())
            sql_segment_rules_cfg = _parse_sql_segment_rules(getattr(config, "segment_sql_ring_rules", {}))
            if config.segment_sql_ring_enabled and config.segment_mode == "classic_loop" and sql_segment_rules_cfg:
                logging.warning("segment_sql_ring ignored because segment_mode=classic_loop")
            for inst in installs:
                # Mautic 6 core hotfix: ReloadHelper may pass null metadata to
                # PluginUpdateEvent (strict array in M6), which breaks
                # plugin install/reload. Keep this check idempotent and always
                # active so version upgrades that overwrite the file are
                # auto-patched again on next planning cycle.
                try:
                    patch_gate = should_apply_m6_plugin_update_metadata_patch(
                        inst,
                        policy=config.mautic6_core_patch_policy,
                        version_min=config.mautic6_core_patch_version_min,
                        version_max=config.mautic6_core_patch_version_max,
                        apply_if_version_unknown=bool(config.mautic6_core_patch_apply_if_version_unknown),
                    )
                    if bool(patch_gate.get("apply", False)):
                        patch_res = ensure_m6_plugin_update_metadata_patch(inst)
                        p_status = str(patch_res.get("status", "")).strip().lower()
                        if p_status == "patched":
                            logging.info("[%s] mautic6 core patch applied: %s", inst.root, patch_res.get("path", "-"))
                        elif p_status == "error":
                            logging.warning("[%s] mautic6 core patch error: %s", inst.root, patch_res.get("reason", "unknown"))
                    else:
                        reason = str(patch_gate.get("reason", "")).strip()
                        version = str(patch_gate.get("version", "")).strip() or "-"
                        if reason not in {"policy_off", "not_mautic_6"}:
                            logging.info("[%s] mautic6 core patch skipped: reason=%s version=%s", inst.root, reason or "n/a", version)
                except Exception as e:
                    logging.warning("[%s] mautic6 core patch check failed: %s", inst.root, e)

                if str(getattr(config, "pagehit_cascade_patch_policy", "required") or "required").strip().lower() == "required":
                    try:
                        pagehit_patch_res = ensure_pagehit_cascade_patch(inst)
                        ph_status = str(pagehit_patch_res.get("status", "")).strip().lower()
                        if ph_status == "patched":
                            logging.info(
                                "[%s] pagehit cascade patch applied: model=%s handler=%s",
                                inst.root,
                                pagehit_patch_res.get("page_model", "-"),
                                pagehit_patch_res.get("handler", "-"),
                            )
                        elif ph_status == "error":
                            logging.warning(
                                "[%s] pagehit cascade patch error: %s",
                                inst.root,
                                pagehit_patch_res.get("reason", "unknown"),
                            )
                    except Exception as e:
                        logging.warning("[%s] pagehit cascade patch check failed: %s", inst.root, e)

                if not inst.db:
                    continue
                root = inst.root
                db = MauticDB(inst.db)
                inst_now = now_utc
                if inst.mautic_timezone:
                    try:
                        inst_now = now_utc.astimezone(ZoneInfo(inst.mautic_timezone))
                    except Exception:
                        logging.warning("[%s] invalid mautic timezone in local.php: %s", root, inst.mautic_timezone)
                sql_ctx = {
                    "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    "now_local": inst_now.strftime("%Y-%m-%d %H:%M:%S"),
                    "window_start_utc_24h": (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
                    "window_start_local_24h": (inst_now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
                }
                sql_ring_enabled_for_root = bool(
                    config.segment_sql_ring_enabled and config.segment_mode != "classic_loop"
                )
                sql_rules_for_root: dict[int, SQLSegmentRule] = (
                    dict(sql_segment_rules_cfg) if sql_ring_enabled_for_root else {}
                )

                segment_ids: list[int] | None = None
                campaign_trigger_ids: list[int] | None = None
                campaign_rebuild_ids: list[int] | None = None
                if now - last_import_poll_ts.get(root, 0.0) >= max(1, config.import_poll_interval_sec):
                    try:
                        import_pending_cache[root] = db.fetch_count(config.sql_import_pending_count, context=sql_ctx)
                    except Exception as e:
                        logging.warning("[%s] import query failed: %s", root, e)
                        if _is_db_dispatch_pause_error(e):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=f"import planning db error: {e}",
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                            )
                        import_pending_cache[root] = 0
                    last_import_poll_ts[root] = now
                import_pending_now = max(0, int(import_pending_cache.get(root, 0)))
                import_force_until = float(segment_force_full_scan_until.get(root, 0.0))
                import_hold_sec = max(120, int(config.import_poll_interval_sec) * 8)
                if import_pending_now > 0:
                    import_force_until = max(import_force_until, now + float(import_hold_sec))
                    segment_force_full_scan_until[root] = import_force_until
                force_segment_full_scan = now < import_force_until
                full_scan_interval_sec = max(0, int(getattr(config, "segment_full_scan_interval_sec", 300) or 0))
                if (
                    not force_segment_full_scan
                    and full_scan_interval_sec > 0
                    and now - float(segment_last_full_scan_ts.get(root, 0.0)) >= float(full_scan_interval_sec)
                ):
                    force_segment_full_scan = True
                try:
                    if force_segment_full_scan:
                        segment_ids = db.fetch_ids(_SQL_SEGMENTS_ALL_PUBLISHED, limit=5000, context=sql_ctx)
                        segment_last_full_scan_ts[root] = now
                        logging.info(
                            "[%s] segment full-scan planned (reason=%s pending_imports=%s interval=%ss)",
                            root,
                            "import_activity" if now < import_force_until else "periodic_full_scan",
                            import_pending_now,
                            full_scan_interval_sec,
                        )
                    else:
                        segment_ids = db.fetch_ids(config.sql_segments_due, limit=5000, context=sql_ctx)
                except Exception as e:
                    logging.warning("[%s] segment query failed: %s", root, e)
                    if _is_db_dispatch_pause_error(e):
                        _mark_db_dispatch_pause(
                            root=root,
                            reason=f"segment planning db error: {e}",
                            now_ts=now,
                            pause_until=db_dispatch_pause_until,
                            pause_reasons=db_dispatch_pause_reasons,
                        )

                campaign_query_error: Exception | None = None
                try:
                    campaign_triggers_due_sql = _campaign_sql_for_major(
                        config.sql_campaign_triggers_due,
                        inst.mautic_major,
                    )
                    campaign_trigger_ids = db.fetch_ids(campaign_triggers_due_sql, limit=5000, context=sql_ctx)
                except Exception as e:
                    campaign_query_error = e
                    logging.warning("[%s] campaign trigger query failed: %s", root, e)
                if config.enable_campaign_rebuild:
                    try:
                        campaign_rebuilds_due_sql = _campaign_sql_for_major(
                            config.sql_campaign_rebuilds_due,
                            inst.mautic_major,
                        )
                        campaign_rebuild_ids = db.fetch_ids(campaign_rebuilds_due_sql, limit=5000, context=sql_ctx)
                    except Exception as e:
                        campaign_query_error = e
                        logging.warning("[%s] campaign rebuild query failed: %s", root, e)
                else:
                    campaign_rebuild_ids = []

                if campaign_query_error is not None and _is_db_dispatch_pause_error(campaign_query_error):
                    _mark_db_dispatch_pause(
                        root=root,
                        reason=f"campaign planning db error: {campaign_query_error}",
                        now_ts=now,
                        pause_until=db_dispatch_pause_until,
                        pause_reasons=db_dispatch_pause_reasons,
                    )

                if campaign_trigger_ids is not None and (config.profile_name or "").strip().lower() == "tiny":
                    # Tiny mode: newest published campaigns first.
                    campaign_trigger_ids = sorted(list(dict.fromkeys(campaign_trigger_ids)), reverse=True)
                if campaign_rebuild_ids is not None and (config.profile_name or "").strip().lower() == "tiny":
                    campaign_rebuild_ids = sorted(list(dict.fromkeys(campaign_rebuild_ids)), reverse=True)

                if segment_ids is not None:
                    standard_segment_ids = list(dict.fromkeys(segment_ids))
                    if sql_ring_enabled_for_root and sql_rules_for_root:
                        sql_ring_plan = _plan_sql_segment_ring(standard_segment_ids, sql_rules_for_root)
                        active_sql_set = set(sql_ring_plan)
                        segment_sql_rings[root] = _reconcile_ring(segment_sql_rings.get(root), sql_ring_plan)
                        segment_sql_rules_by_root[root] = sql_rules_for_root
                        segment_sql_active_sets[root] = active_sql_set
                        prev_done = set(segment_sql_done_sets.get(root, set()))
                        segment_sql_done_sets[root] = {sid for sid in prev_done if sid in active_sql_set}
                        standard_segment_ids = [sid for sid in standard_segment_ids if sid not in active_sql_set]
                    else:
                        segment_sql_rings[root] = deque()
                        segment_sql_rules_by_root[root] = {}
                        segment_sql_active_sets[root] = set()
                        segment_sql_done_sets[root] = set()

                    segment_weight_rows: list[dict[str, object]] = []
                    seg_w: dict[int, float] = {}
                    stale_seg_ids: set[int] = set()
                    if standard_segment_ids:
                        try:
                            segment_weight_rows = db.fetch_rows(config.sql_segment_weights, limit=5000, context=sql_ctx)
                        except Exception as e:
                            logging.warning("[%s] segment weight query failed: %s", root, e)
                            segment_weight_rows = []

                        seg_w = store.get_weights("segment", root, config.weights_recalc_interval_sec)
                        if _needs_weight_recalc(standard_segment_ids, seg_w):
                            seg_w = _segment_weights(standard_segment_ids, segment_weight_rows, segment_whitelist, now_ts)
                            store.put_weights("segment", root, seg_w)
                        stale_seg_ids = _stale_segment_priority_ids(
                            standard_segment_ids,
                            segment_weight_rows,
                            now_ts,
                            stale_sec=_SEGMENT_STALE_PRIORITY_SEC,
                        )
                    else:
                        store.put_weights("segment", root, {})

                    seg_prio, seg_reg = _split_segment_circles(
                        standard_segment_ids,
                        seg_w,
                        segment_whitelist,
                        config.segment_priority_weight_threshold,
                        config.segment_priority_size,
                        stale_priority_ids=stale_seg_ids,
                    )
                    if config.ring_mode == "single":
                        seg_prio, seg_reg = [], list(dict.fromkeys(standard_segment_ids))
                    if not _partition_complete(standard_segment_ids, seg_prio, seg_reg):
                        logging.warning("[%s] invalid segment partition, forcing single ring", root)
                        seg_prio, seg_reg = [], sorted(list(dict.fromkeys(standard_segment_ids)))
                    segment_prio_rings[root] = _reconcile_ring(segment_prio_rings.get(root), seg_prio)
                    segment_reg_rings[root] = _reconcile_ring(segment_reg_rings.get(root), seg_reg)
                    segment_prio_sets[root] = set(seg_prio)
                    segment_reg_sets[root] = set(seg_reg)
                else:
                    logging.warning("[%s] segment planning skipped: preserving previous segment rings", root)

                if campaign_trigger_ids is not None and campaign_rebuild_ids is not None:
                    campaign_trigger_ids = list(dict.fromkeys(campaign_trigger_ids))
                    campaign_rebuild_ids = list(dict.fromkeys(campaign_rebuild_ids))
                    campaign_all_ids = list(dict.fromkeys(campaign_trigger_ids + campaign_rebuild_ids))
                    camp_w = store.get_weights("campaign", root, config.weights_recalc_interval_sec)
                    campaign_weight_rows: list[dict[str, object]] = []
                    campaign_weight_query_failed = False
                    if _needs_weight_recalc(campaign_all_ids, camp_w):
                        try:
                            campaign_weights_sql = _campaign_sql_for_major(config.sql_campaign_weights, inst.mautic_major)
                            campaign_weight_rows = db.fetch_rows(campaign_weights_sql, limit=5000, context=sql_ctx)
                        except Exception as e:
                            logging.warning("[%s] campaign weight query failed: %s", root, e)
                            campaign_weight_query_failed = True
                            campaign_weight_rows = []
                        camp_w = _campaign_weights(campaign_all_ids, campaign_weight_rows, campaign_whitelist, now_ts)
                        store.put_weights("campaign", root, camp_w)
                    latest_priority_ids = _latest_campaign_ids(campaign_weight_rows, config.campaign_latest_priority_count)
                    if not latest_priority_ids:
                        # If weights are cached (no recalc) or weight query failed,
                        # keep newest published campaigns in priority lane using
                        # id-based fallback without issuing extra heavy SQL.
                        latest_priority_ids = _latest_campaign_ids_from_ids(campaign_all_ids, config.campaign_latest_priority_count)
                    if campaign_weight_query_failed:
                        logging.info("[%s] campaign latest-priority fallback to id order", root)

                    trg_prio, trg_reg = _split_campaign_circles(
                        campaign_trigger_ids,
                        camp_w,
                        campaign_whitelist,
                        config.campaign_priority_size,
                        latest_priority_ids,
                    )
                    reb_prio, reb_reg = _split_campaign_circles(
                        campaign_rebuild_ids,
                        camp_w,
                        campaign_whitelist,
                        config.campaign_priority_size,
                        latest_priority_ids,
                    )
                    if config.ring_mode == "single":
                        trg_prio, trg_reg = [], list(dict.fromkeys(campaign_trigger_ids))
                        reb_prio, reb_reg = [], list(dict.fromkeys(campaign_rebuild_ids))
                    if not _partition_complete(campaign_trigger_ids, trg_prio, trg_reg):
                        logging.warning("[%s] invalid campaign trigger partition, forcing single ring", root)
                        trg_prio, trg_reg = [], sorted(list(dict.fromkeys(campaign_trigger_ids)))
                    if not _partition_complete(campaign_rebuild_ids, reb_prio, reb_reg):
                        logging.warning("[%s] invalid campaign rebuild partition, forcing single ring", root)
                        reb_prio, reb_reg = [], sorted(list(dict.fromkeys(campaign_rebuild_ids)))
                    campaign_trigger_prio_rings[root] = _reconcile_ring(campaign_trigger_prio_rings.get(root), trg_prio)
                    campaign_trigger_reg_rings[root] = _reconcile_ring(campaign_trigger_reg_rings.get(root), trg_reg)
                    campaign_rebuild_prio_rings[root] = _reconcile_ring(campaign_rebuild_prio_rings.get(root), reb_prio)
                    campaign_rebuild_reg_rings[root] = _reconcile_ring(campaign_rebuild_reg_rings.get(root), reb_reg)
                    campaign_trigger_prio_sets[root] = set(trg_prio)
                    campaign_trigger_reg_sets[root] = set(trg_reg)
                    campaign_rebuild_prio_sets[root] = set(reb_prio)
                    campaign_rebuild_reg_sets[root] = set(reb_reg)
                else:
                    _clear_campaign_rings(
                        root=root,
                        trigger_prio_rings=campaign_trigger_prio_rings,
                        trigger_reg_rings=campaign_trigger_reg_rings,
                        rebuild_prio_rings=campaign_rebuild_prio_rings,
                        rebuild_reg_rings=campaign_rebuild_reg_rings,
                        trigger_prio_sets=campaign_trigger_prio_sets,
                        trigger_reg_sets=campaign_trigger_reg_sets,
                        rebuild_prio_sets=campaign_rebuild_prio_sets,
                        rebuild_reg_sets=campaign_rebuild_reg_sets,
                    )
                    logging.warning("[%s] campaign planning skipped: campaign rings cleared", root)

                # Use freshly planned rings/sets in the same tick.
                # Without rebinding, dispatch operates on previous-cycle objects
                # (captured before reconcile), which can skew 3+1 behavior.
                seg_prio_ring = segment_prio_rings.setdefault(root, deque())
                seg_reg_ring = segment_reg_rings.setdefault(root, deque())
                trg_prio_ring = campaign_trigger_prio_rings.setdefault(root, deque())
                trg_reg_ring = campaign_trigger_reg_rings.setdefault(root, deque())
                reb_prio_ring = campaign_rebuild_prio_rings.setdefault(root, deque())
                reb_reg_ring = campaign_rebuild_reg_rings.setdefault(root, deque())
                seg_prio_set = segment_prio_sets.setdefault(root, set(seg_prio_ring))
                seg_reg_set = segment_reg_sets.setdefault(root, set(seg_reg_ring))
                trg_prio_set = campaign_trigger_prio_sets.setdefault(root, set(trg_prio_ring))
                trg_reg_set = campaign_trigger_reg_sets.setdefault(root, set(trg_reg_ring))
                reb_prio_set = campaign_rebuild_prio_sets.setdefault(root, set(reb_prio_ring))
                reb_reg_set = campaign_rebuild_reg_sets.setdefault(root, set(reb_reg_ring))

                q_samples = queue_samples.setdefault(root, deque())
                if config.disable_throttle:
                    throttled[root] = False
                else:
                    try:
                        queue_count = db.fetch_count(config.sql_mail_queue_count, context=sql_ctx)
                    except Exception as e:
                        logging.warning("[%s] mail queue query failed: %s", root, e)
                        if _is_db_dispatch_pause_error(e):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=f"mail queue db error: {e}",
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                            )
                        queue_count = 0
                    q_samples.append((now, queue_count))
                    throttled[root] = _compute_throttle_active(
                        q_samples,
                        threshold=config.queue_throttle_threshold,
                        window_min=config.queue_throttle_window_min,
                    )

            next_plan_refresh_at = now + max(1, config.poll_interval_sec)

        if config.fs_permissions_guard_enabled:
            guard_interval_sec = max(15, int(config.fs_permissions_guard_interval_sec or 300))
            for inst in installs:
                root = str(inst.root or "").strip()
                if not root:
                    continue
                last_ts = float(last_fs_permissions_guard_ts.get(root, 0.0))
                if last_ts > 0 and now - last_ts < guard_interval_sec:
                    continue
                try:
                    res = ensure_instance_permissions(
                        root=root,
                        run_as_user=config.mautic_run_as_user or "www-data",
                        guard_paths=config.fs_permissions_guard_paths,
                        fix_console_exec=bool(config.fs_permissions_guard_fix_console_exec),
                        console_relpath=config.fs_permissions_guard_console_relpath,
                    )
                    if res.repaired_paths or res.console_exec_fixed:
                        fix_count = len(res.repaired_paths) + (1 if res.console_exec_fixed else 0)
                        repaired_events = [e for e in (res.repair_events or []) if bool(getattr(e, "repaired", False))]
                        event_payload = [
                            {
                                "ts": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "path": str(getattr(e, "rel_path", "") or getattr(e, "target_path", "") or "").strip(),
                                "sample_path": str(getattr(e, "sample_path", "") or "").strip(),
                                "reason": str(getattr(e, "reason", "") or "").strip(),
                                "actor": str(getattr(e, "actor", "") or "").strip(),
                                "actor_source": str(getattr(e, "actor_source", "") or "").strip(),
                                "before_owner_group": str(getattr(e, "before_owner_group", "") or "").strip(),
                                "before_mode": str(getattr(e, "before_mode", "") or "").strip(),
                                "result": "repaired",
                                "error": "",
                            }
                            for e in repaired_events
                        ]
                        try:
                            pusher.add_fs_permissions_fix(
                                fix_count,
                                events=event_payload,
                                now_ts=now,
                            )
                        except Exception:
                            pass
                        detail_parts: list[str] = []
                        for ev in repaired_events[:8]:
                            detail_parts.append(
                                (
                                    f"path={ev.rel_path or ev.target_path} "
                                    f"reason={ev.reason or '-'} "
                                    f"actor={ev.actor or 'unknown'} "
                                    f"source={ev.actor_source or 'unknown'} "
                                    f"before={ev.before_owner_group or '-'} mode={ev.before_mode or '-'}"
                                )
                            )
                        logging.warning(
                            "[%s] fs permissions guard repaired count=%s paths=%s console_exec_fixed=%s details=%s",
                            root,
                            str(fix_count),
                            ",".join(res.repaired_paths) if res.repaired_paths else "-",
                            "yes" if res.console_exec_fixed else "no",
                            " || ".join(detail_parts) if detail_parts else "-",
                        )
                    if res.errors:
                        failed_parts: list[str] = []
                        for ev in (res.repair_events or []):
                            if bool(getattr(ev, "repaired", False)):
                                continue
                            err = str(getattr(ev, "error", "") or "").strip()
                            if not err:
                                continue
                            failed_parts.append(
                                (
                                    f"path={ev.rel_path or ev.target_path} "
                                    f"reason={ev.reason or '-'} "
                                    f"actor={ev.actor or 'unknown'} "
                                    f"source={ev.actor_source or 'unknown'} "
                                    f"error={err}"
                                )
                            )
                        logging.warning(
                            "[%s] fs permissions guard errors: %s%s",
                            root,
                            " | ".join(res.errors),
                            f" || details={' || '.join(failed_parts[:8])}" if failed_parts else "",
                        )
                except Exception as e:
                    logging.warning("[%s] fs permissions guard failed: %s", root, e)
                last_fs_permissions_guard_ts[root] = now

        db_watchdog_cfg = effective_db_watchdog_config(config)
        if bool(db_watchdog_cfg.get("enabled", False)):
            db_watchdog_interval_sec = max(30, int(db_watchdog_cfg.get("interval_sec", 300) or 300))
            for inst in installs:
                root = str(inst.root or "").strip()
                if not root:
                    continue
                last_ts = float(last_db_watchdog_ts.get(root, 0.0))
                if last_ts > 0 and now - last_ts < db_watchdog_interval_sec:
                    continue
                try:
                    running_types = [t.task_type for t in running.values() if t.root == root]
                    snap = collect_db_watchdog_snapshot(
                        cfg=config,
                        inst=inst,
                        running_task_types=running_types,
                    )
                    pusher.add_db_watchdog_observation(snap, now_ts=now)
                    if str(snap.get("status", "")).strip().lower() == "ok":
                        pl = snap.get("processlist") if isinstance(snap.get("processlist"), dict) else {}
                        rule = snap.get("rules") if isinstance(snap.get("rules"), dict) else {}
                        lock_waits = int(pl.get("metadata_lock_waits", 0) or 0)
                        long_q = int(pl.get("long_queries", 0) or 0)
                        orphans = int(pl.get("orphan_candidates", 0) or 0)
                        rule_hits = int(rule.get("hit_total", 0) or 0)
                        if lock_waits > 0 or long_q > 0 or orphans > 0 or rule_hits > 0:
                            logging.warning(
                                "[%s] db_watchdog observe: metadata_lock_waits=%s long_queries=%s orphan_candidates=%s rule_hits=%s",
                                root,
                                lock_waits,
                                long_q,
                                orphans,
                                rule_hits,
                            )
                        long_q_threshold = int(
                            db_watchdog_cfg.get(
                                "long_queries_pause_threshold",
                                _DB_WATCHDOG_LONG_QUERIES_PAUSE_THRESHOLD,
                            )
                            or 0
                        )
                        lock_threshold = int(
                            db_watchdog_cfg.get(
                                "metadata_lock_waits_pause_threshold",
                                _DB_WATCHDOG_METADATA_LOCKS_PAUSE_THRESHOLD,
                            )
                            or 0
                        )
                        if (long_q_threshold > 0 and long_q >= long_q_threshold) or (
                            lock_threshold > 0 and lock_waits >= lock_threshold
                        ):
                            _mark_db_dispatch_pause(
                                root=root,
                                reason=(
                                    "db_watchdog overload: "
                                    f"metadata_lock_waits={lock_waits} long_queries={long_q}"
                                ),
                                now_ts=now,
                                pause_until=db_dispatch_pause_until,
                                pause_reasons=db_dispatch_pause_reasons,
                                pause_sec=int(db_watchdog_cfg.get("dispatch_pause_sec", _DB_DISPATCH_PAUSE_SEC) or _DB_DISPATCH_PAUSE_SEC),
                            )
                    else:
                        logging.warning("[%s] db_watchdog collect skipped/error: %s", root, snap.get("reason", "-"))
                except Exception as e:
                    logging.warning("[%s] db_watchdog collect failed: %s", root, e)
                last_db_watchdog_ts[root] = now

        if pusher.enabled():
            pending_profile_event = read_pending_profile_event(config)
            payload = pusher.build_payload(
                installs=installs,
                profile_name=config.profile_name,
                now_ts=now,
                profile_event=pending_profile_event,
            )
            payload_no_ts = dict(payload)
            payload_no_ts.pop("sent_at_utc", None)
            do_push, _changed = pusher.should_push(now, payload_no_ts)
            if pending_profile_event:
                do_push = True
            if do_push:
                ok, msg = pusher.send(payload)
                log_push_result(ok, msg)
                if ok:
                    pusher.mark_pushed(now, payload_no_ts)
                if pending_profile_event:
                    clear_pending_profile_event(
                        config,
                        event_id=str(pending_profile_event.get("event_id", "")).strip() or None,
                        delivered=bool(ok),
                        error=None if ok else str(msg),
                    )

        if (
            bool(getattr(config, "mcc_profile_guard_enabled", False))
            and config.mcc_url
            and config.mcc_token
            and now >= next_profile_guard_at
        ):
            guard_interval = max(60, int(config.mcc_push_interval_sec or config.poll_interval_sec or 300))
            try:
                drift = check_profile_drift_with_mcc(
                    config.config_file_path,
                    current_profile=(config.profile_name or ""),
                    current_config_sha=(config.config_sha256 or ""),
                )
                if str(drift.get("status", "")).strip().lower() == "drift":
                    old_profile = (config.profile_name or "").strip().lower() or None
                    desired_profile = str(drift.get("desired_profile", "")).strip().lower() or None
                    ok, note = recover_config_from_mcc(
                        config.config_file_path,
                        reason=(
                            "profile_drift"
                            f" local={old_profile or '-'} desired={desired_profile or '-'}"
                            f" source={str(drift.get('config_source', '-') or '-')}"
                        ),
                    )
                    if ok:
                        logging.error(
                            "profile drift detected and repaired from MCC desired config: local=%s desired=%s (%s)",
                            old_profile or "-",
                            desired_profile or "-",
                            note,
                        )
                        config = load_config(config.config_file_path, allow_recover_from_mcc=False)
                        base_config = config
                        pusher.cfg = config
                        next_plan_refresh_at = 0.0
                        next_update_check_at = 0.0
                        next_service_profile_apply_at = 0.0
                        next_runtime_overrides_poll_at = 0.0
                        segment_whitelist = set(config.segment_whitelist) | _load_id_file(config.segment_whitelist_file)
                        campaign_whitelist = set(config.campaign_whitelist) | _load_id_file(config.campaign_whitelist_file)
                        if config.disable_whitelist:
                            segment_whitelist = set()
                            campaign_whitelist = set()
                        try:
                            queue_profile_event(
                                config,
                                source="auto_recover",
                                initiated_by_user=False,
                                old_profile=old_profile,
                                new_profile=(config.profile_name or "").strip().lower() or desired_profile,
                                reason="auto_recover_profile_drift",
                                details={
                                    "desired_profile": desired_profile,
                                    "mcc_config_source": str(drift.get("config_source", "")).strip() or None,
                                },
                            )
                        except Exception as e:
                            logging.warning("profile-drift repair event enqueue failed: %s", e)
                    else:
                        logging.error(
                            "profile drift detected but MCC repair failed: local=%s desired=%s err=%s",
                            old_profile or "-",
                            desired_profile or "-",
                            note,
                        )
            except Exception as e:
                logging.warning("profile guard check failed: %s", e)
            next_profile_guard_at = now + guard_interval

        if (config.profile_name or "").strip().lower() == "passive":
            logging.info("Passive profile active: planning-only mode (no task dispatch)")
            if single_cycle:
                return
            time.sleep(max(0.1, float(config.dispatch_interval_sec)))
            continue

        scheduler_dispatch_pause = False
        try:
            scheduler_dispatch_pause = Path(config.scheduler_pause_flag_path).exists()
        except Exception as e:
            logging.warning("scheduler pause flag check failed: %s", e)
            scheduler_dispatch_pause = False
        if scheduler_dispatch_pause != scheduler_dispatch_pause_active:
            if scheduler_dispatch_pause:
                logging.info(
                    "scheduler pause active (%s): new task dispatch paused for all task types (running tasks continue)",
                    config.scheduler_pause_flag_path,
                )
            else:
                logging.info("scheduler pause released: task dispatch resumed")
            scheduler_dispatch_pause_active = scheduler_dispatch_pause

        backup_running = False
        if config.backup_enabled:
            try:
                backup_running = bool(backup_thread is not None and backup_thread.is_alive()) or backup_lock_active(config)
            except Exception as e:
                logging.warning("backup lock check failed: %s", e)
                backup_running = False
        backup_dispatch_pause, backup_pause_reason = _backup_dispatch_pause_state(
            config,
            backup_running=backup_running,
        )
        if backup_dispatch_pause != backup_dispatch_pause_active:
            if backup_dispatch_pause:
                logging.info(
                    "backup guard active (%s): new task dispatch paused for all task types (running tasks continue)",
                    backup_pause_reason or "unknown",
                )
            else:
                logging.info("backup guard released: task dispatch resumed")
            backup_dispatch_pause_active = backup_dispatch_pause
        dispatch_pause = bool(scheduler_dispatch_pause or backup_dispatch_pause)

        for inst in installs:
            if not inst.db:
                logging.warning("[%s] skip install without db config", inst.root)
                continue

            root = inst.root
            db = MauticDB(inst.db)
            now_utc = datetime.now(timezone.utc)
            inst_now = now_utc
            if inst.mautic_timezone:
                try:
                    inst_now = now_utc.astimezone(ZoneInfo(inst.mautic_timezone))
                except Exception:
                    inst_now = now_utc
            sql_ctx = {
                "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                "now_local": inst_now.strftime("%Y-%m-%d %H:%M:%S"),
                "window_start_utc_24h": (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
                "window_start_local_24h": (inst_now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
            }
            seg_sql_ring = segment_sql_rings.setdefault(root, deque())
            seg_sql_rules = segment_sql_rules_by_root.setdefault(root, {})
            seg_sql_active = segment_sql_active_sets.setdefault(root, set())
            seg_sql_done = segment_sql_done_sets.setdefault(root, set())
            seg_prio_ring = segment_prio_rings.setdefault(root, deque())
            seg_reg_ring = segment_reg_rings.setdefault(root, deque())
            trg_prio_ring = campaign_trigger_prio_rings.setdefault(root, deque())
            trg_reg_ring = campaign_trigger_reg_rings.setdefault(root, deque())
            reb_prio_ring = campaign_rebuild_prio_rings.setdefault(root, deque())
            reb_reg_ring = campaign_rebuild_reg_rings.setdefault(root, deque())
            seg_prio_set = segment_prio_sets.setdefault(root, set())
            seg_reg_set = segment_reg_sets.setdefault(root, set())
            trg_prio_set = campaign_trigger_prio_sets.setdefault(root, set())
            trg_reg_set = campaign_trigger_reg_sets.setdefault(root, set())
            reb_prio_set = campaign_rebuild_prio_sets.setdefault(root, set())
            reb_reg_set = campaign_rebuild_reg_sets.setdefault(root, set())

            if dispatch_pause:
                continue
            db_pause_until = float(db_dispatch_pause_until.get(root, 0.0))
            if db_pause_until > now:
                continue
            if db_pause_until > 0:
                logging.info(
                    "[%s] db dispatch circuit-breaker released: %s",
                    root,
                    db_dispatch_pause_reasons.get(root, "-"),
                )
                db_dispatch_pause_until.pop(root, None)
                db_dispatch_pause_reasons.pop(root, None)

            _dispatch_manual_requests_for_root(
                config=config,
                store=store,
                running=running,
                popens=popens,
                root=root,
                seg_sql_ring=seg_sql_ring,
                seg_prio_ring=seg_prio_ring,
                seg_reg_ring=seg_reg_ring,
                trg_prio_ring=trg_prio_ring,
                trg_reg_ring=trg_reg_ring,
                reb_prio_ring=reb_prio_ring,
                reb_reg_ring=reb_reg_ring,
            )

            if config.segment_mode != "classic_loop":
                _run_sql_segment_ring(
                    config=config,
                    store=store,
                    db=db,
                    root=root,
                    ring=seg_sql_ring,
                    rules=seg_sql_rules,
                    active_set=seg_sql_active,
                    done_set=seg_sql_done,
                    running=running,
                    sql_ctx=sql_ctx,
                    now_ts=now,
                    now_local=inst_now,
                )

            if config.segment_mode == "classic_loop":
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_segment_full_update_template,
                    batch_limit=config.segment_batch_limit,
                )
                _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type="segment",
                    entity_id=None,
                    args=args,
                    timeout_sec=config.command_timeout_sec,
                    max_parallel_for_type=1,
                    popens=popens,
                )
            else:
                if throttled.get(root, False):
                    if config.segment_throttle_whitelist_only:
                        seg_prio_limit = max(0, config.segment_throttle_whitelist_parallel)
                        seg_reg_limit = 0
                    else:
                        seg_prio_limit = max(0, config.segment_priority_parallel_throttled)
                        seg_reg_limit = max(0, config.segment_regular_parallel_throttled)
                else:
                    seg_prio_limit = max(0, config.segment_priority_parallel_idle)
                    seg_reg_limit = max(0, config.segment_regular_parallel_idle)
                seg_total_limit = seg_prio_limit + seg_reg_limit
                eff_seg_prio_limit = seg_prio_limit
                eff_seg_reg_limit = seg_reg_limit
                prefer_priority_spill = False
                if (
                    not throttled.get(root, False)
                    and seg_prio_limit > 0
                    and seg_reg_limit > 0
                    and seg_prio_set
                ):
                    prio_backlog = _ring_has_launchable(
                        seg_prio_ring,
                        running=running,
                        root=root,
                        task_type="segment",
                    )
                    stuck_prio = _running_entities_stuck_for(
                        running,
                        root=root,
                        task_type="segment",
                        entity_ids=seg_prio_set,
                        now_ts=now,
                        stuck_sec=_SEGMENT_STUCK_SPILLOVER_SEC,
                    )
                    if prio_backlog and stuck_prio > 0:
                        # Keep 3+1 behavior as default, but when priority is
                        # blocked by long-running tasks we temporarily borrow
                        # regular launch slot for priority backlog.
                        eff_seg_prio_limit = min(seg_total_limit, seg_prio_limit + seg_reg_limit)
                        eff_seg_reg_limit = 0
                        prefer_priority_spill = True
                        logging.warning(
                            "[%s] segment priority spillover active: stuck_prio=%s limits=%s+%s -> %s+%s",
                            root,
                            stuck_prio,
                            seg_prio_limit,
                            seg_reg_limit,
                            eff_seg_prio_limit,
                            eff_seg_reg_limit,
                        )
                seg_resume_ring = segment_resume_rings.setdefault(root, deque())

                if throttled.get(root, False) and config.segment_throttle_whitelist_only and config.segment_throttle_kill_non_whitelist:
                    for key, task in list(running.items()):
                        if task.root != root or task.task_type != "segment":
                            continue
                        if task.entity_id is None or task.entity_id in segment_whitelist:
                            continue
                        _kill_pid(task.pid, config.segment_kill_grace_sec)
                        store.finish(task.row_id, state="timeout", rc=None, note="throttle_kill")
                        running.pop(key, None)
                        popens.pop(key, None)
                        if task.entity_id not in seg_resume_ring:
                            seg_resume_ring.appendleft(task.entity_id)

                if not throttled.get(root, False) and seg_resume_ring:
                    _fill_from_ring(
                        ring=seg_resume_ring,
                        ring_limit=seg_total_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=None,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                    )

                if throttled.get(root, False) and config.segment_throttle_whitelist_only:
                    wl_ids = list(dict.fromkeys([x for x in list(seg_prio_ring) + list(seg_reg_ring) if x in segment_whitelist]))
                    seg_wl_ring = deque(wl_ids)
                    _fill_from_ring(
                        ring=seg_wl_ring,
                        ring_limit=seg_prio_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=segment_whitelist,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                    )
                    seg_cur_total = _running_count(running, root, "segment")
                    if seg_cur_total >= seg_total_limit:
                        pass
                    else:
                        # No non-whitelist launches while throttle is active.
                        pass
                else:
                    _fill_from_ring(
                        ring=seg_prio_ring,
                        ring_limit=eff_seg_prio_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=seg_prio_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                    )
                    _fill_from_ring(
                        ring=seg_reg_ring,
                        ring_limit=eff_seg_reg_limit,
                        total_limit=seg_total_limit,
                        root=root,
                        task_type="segment",
                        running=running,
                        ring_entities=seg_reg_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda sid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_segment_update_template,
                            id=sid,
                            batch_limit=config.segment_batch_limit,
                        ),
                    )
                    seg_cur_total = _running_count(running, root, "segment")
                    if seg_cur_total < seg_total_limit:
                        spill = seg_total_limit - seg_cur_total
                        if prefer_priority_spill and seg_prio_ring and spill > 0:
                            _fill_from_ring(
                                ring=seg_prio_ring,
                                ring_limit=eff_seg_prio_limit,
                                total_limit=seg_total_limit,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=seg_prio_set,
                                config=config,
                                store=store,
                                popens=popens,
                                build_args=lambda sid: render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_segment_update_template,
                                    id=sid,
                                    batch_limit=config.segment_batch_limit,
                                ),
                            )
                        elif seg_reg_ring:
                            _fill_from_ring(
                                ring=seg_reg_ring,
                                ring_limit=spill,
                                total_limit=seg_total_limit,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=seg_reg_set,
                                config=config,
                                store=store,
                                popens=popens,
                                build_args=lambda sid: render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_segment_update_template,
                                    id=sid,
                                    batch_limit=config.segment_batch_limit,
                                ),
                            )
                        elif seg_prio_ring and spill > 0:
                            # If regular ring is empty, keep total segment concurrency at target
                            # by temporarily borrowing regular slot(s) for priority ring.
                            _fill_from_ring(
                                ring=seg_prio_ring,
                                ring_limit=seg_prio_limit + spill,
                                total_limit=seg_total_limit,
                                root=root,
                                task_type="segment",
                                running=running,
                                ring_entities=seg_prio_set,
                                config=config,
                                store=store,
                                popens=popens,
                                build_args=lambda sid: render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_segment_update_template,
                                    id=sid,
                                    batch_limit=config.segment_batch_limit,
                                ),
                            )

            # `mautic:campaigns:update` is treated as synonym of
            # `mautic:campaigns:rebuild` and is not scheduled separately.
            # This avoids duplicate campaign pre-processing passes.
            if (config.profile_name or "").strip().lower() == "tiny":
                if _running_campaign_total(running, root) == 0:
                    next_trigger_id = None
                    if trg_prio_ring:
                        next_trigger_id = trg_prio_ring[0]
                        trg_prio_ring.rotate(-1)
                    elif trg_reg_ring:
                        next_trigger_id = trg_reg_ring[0]
                        trg_reg_ring.rotate(-1)

                    if next_trigger_id is not None:
                        _submit_if_slot(
                            config=config,
                            store=store,
                            running=running,
                            root=root,
                            task_type="campaign_trigger",
                            entity_id=next_trigger_id,
                            args=render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_campaign_trigger_template,
                                id=next_trigger_id,
                                campaign_limit=config.campaign_limit,
                                batch_limit=config.campaign_batch_limit,
                            ),
                            timeout_sec=config.command_timeout_sec,
                            max_parallel_for_type=1,
                            popens=popens,
                        )
                    else:
                        next_campaign_id = None
                        if reb_prio_ring:
                            next_campaign_id = reb_prio_ring[0]
                            reb_prio_ring.rotate(-1)
                        elif reb_reg_ring:
                            next_campaign_id = reb_reg_ring[0]
                            reb_reg_ring.rotate(-1)
                        if next_campaign_id is not None:
                            _submit_if_slot(
                                config=config,
                                store=store,
                                running=running,
                                root=root,
                                task_type="campaign_rebuild",
                                entity_id=next_campaign_id,
                                args=render_mautic_command(
                                    php_bin=config.php_bin,
                                    run_as_user=config.mautic_run_as_user,
                                    root=root,
                                    template=config.cmd_campaign_rebuild_template,
                                    id=next_campaign_id,
                                ),
                                timeout_sec=config.command_timeout_sec,
                                max_parallel_for_type=1,
                                popens=popens,
                            )
            # Tiny mode has a single campaign worker:
            # - trigger-due campaigns first
            # - then rebuild-due campaigns
            # Import polling must stay independent from campaign slot occupancy.
            if (config.profile_name or "").strip().lower() == "tiny":
                if config.enable_import_polling and import_pending_cache.get(root, 0) > 0:
                    args = render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=config.cmd_import_template,
                        import_limit=config.import_limit,
                    )
                    _submit_if_slot(
                        config=config,
                        store=store,
                        running=running,
                        root=root,
                        task_type="import",
                        entity_id=None,
                        args=args,
                        timeout_sec=config.command_timeout_sec,
                        max_parallel_for_type=1,
                        popens=popens,
                    )
                # Skip generic multi-ring campaign scheduler.
                continue

            shared_campaign_cap = max(0, config.campaign_total_parallel)
            rr = campaign_round_robin.get(root, 0)
            trigger_lane_configured = (
                max(0, int(config.campaign_trigger_priority_parallel))
                + max(0, int(config.campaign_trigger_regular_parallel))
            ) > 0
            prefer_rebuild = (
                shared_campaign_cap > 0
                and config.enable_campaign_rebuild
                and (rr % 2 == 1)
            )
            trg_prio_limit = max(0, config.campaign_trigger_priority_parallel)
            trg_reg_limit = max(0, config.campaign_trigger_regular_parallel)
            trg_total_limit = trg_prio_limit + trg_reg_limit
            if shared_campaign_cap > 0:
                rem = max(0, shared_campaign_cap - _running_campaign_total(running, root))
                if prefer_rebuild:
                    rem = 0
                trg_total_limit = min(trg_total_limit, rem)
                trg_prio_limit = min(trg_prio_limit, trg_total_limit)
                trg_reg_limit = min(trg_reg_limit, max(0, trg_total_limit - trg_prio_limit))
            _fill_from_ring(
                ring=trg_prio_ring,
                ring_limit=trg_prio_limit,
                total_limit=trg_total_limit,
                root=root,
                task_type="campaign_trigger",
                running=running,
                ring_entities=trg_prio_set,
                config=config,
                store=store,
                popens=popens,
                build_args=lambda cid: render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_campaign_trigger_template,
                    id=cid,
                    campaign_limit=config.campaign_limit,
                    batch_limit=config.campaign_batch_limit,
                ),
            )
            _fill_from_ring(
                ring=trg_reg_ring,
                ring_limit=trg_reg_limit,
                total_limit=trg_total_limit,
                root=root,
                task_type="campaign_trigger",
                running=running,
                ring_entities=trg_reg_set,
                config=config,
                store=store,
                popens=popens,
                build_args=lambda cid: render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_campaign_trigger_template,
                    id=cid,
                    campaign_limit=config.campaign_limit,
                    batch_limit=config.campaign_batch_limit,
                ),
            )
            trg_cur_total = _running_count(running, root, "campaign_trigger")
            if trg_cur_total < trg_total_limit:
                spill = trg_total_limit - trg_cur_total
                if trg_reg_ring:
                    _fill_from_ring(
                        ring=trg_reg_ring,
                        ring_limit=spill,
                        total_limit=trg_total_limit,
                        root=root,
                        task_type="campaign_trigger",
                        running=running,
                        ring_entities=trg_reg_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda cid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_campaign_trigger_template,
                            id=cid,
                            campaign_limit=config.campaign_limit,
                            batch_limit=config.campaign_batch_limit,
                        ),
                    )
                elif trg_prio_ring:
                    _fill_from_ring(
                        ring=trg_prio_ring,
                        ring_limit=spill,
                        total_limit=trg_total_limit,
                        root=root,
                        task_type="campaign_trigger",
                        running=running,
                        ring_entities=trg_prio_set,
                        config=config,
                        store=store,
                        popens=popens,
                        build_args=lambda cid: render_mautic_command(
                            php_bin=config.php_bin,
                            run_as_user=config.mautic_run_as_user,
                            root=root,
                            template=config.cmd_campaign_trigger_template,
                            id=cid,
                            campaign_limit=config.campaign_limit,
                            batch_limit=config.campaign_batch_limit,
                        ),
                    )
            if config.enable_campaign_rebuild:
                rebuild_prio_limit = max(0, config.campaign_rebuild_priority_parallel)
                rebuild_reg_limit = max(0, config.campaign_rebuild_regular_parallel)
                rebuild_total_limit = rebuild_prio_limit + rebuild_reg_limit
                if shared_campaign_cap > 0:
                    rem = max(0, shared_campaign_cap - _running_campaign_total(running, root))
                    if (not prefer_rebuild) and (trg_prio_limit + trg_reg_limit) > 0:
                        rem = 0
                    rebuild_total_limit = min(rebuild_total_limit, rem)
                    rebuild_prio_limit = min(rebuild_prio_limit, rebuild_total_limit)
                    rebuild_reg_limit = min(rebuild_reg_limit, max(0, rebuild_total_limit - rebuild_prio_limit))
                _fill_from_ring(
                    ring=reb_prio_ring,
                    ring_limit=rebuild_prio_limit,
                    total_limit=rebuild_total_limit,
                    root=root,
                    task_type="campaign_rebuild",
                    running=running,
                    ring_entities=reb_prio_set,
                    config=config,
                    store=store,
                    popens=popens,
                    build_args=lambda cid: render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=config.cmd_campaign_rebuild_template,
                        id=cid,
                    ),
                )
                _fill_from_ring(
                    ring=reb_reg_ring,
                    ring_limit=rebuild_reg_limit,
                    total_limit=rebuild_total_limit,
                    root=root,
                    task_type="campaign_rebuild",
                    running=running,
                    ring_entities=reb_reg_set,
                    config=config,
                    store=store,
                    popens=popens,
                    build_args=lambda cid: render_mautic_command(
                        php_bin=config.php_bin,
                        run_as_user=config.mautic_run_as_user,
                        root=root,
                        template=config.cmd_campaign_rebuild_template,
                        id=cid,
                    ),
                )
                reb_cur_total = _running_count(running, root, "campaign_rebuild")
                if reb_cur_total < rebuild_total_limit:
                    spill = rebuild_total_limit - reb_cur_total
                    if reb_reg_ring:
                        _fill_from_ring(
                            ring=reb_reg_ring,
                            ring_limit=spill,
                            total_limit=rebuild_total_limit,
                            root=root,
                            task_type="campaign_rebuild",
                            running=running,
                            ring_entities=reb_reg_set,
                            config=config,
                            store=store,
                            popens=popens,
                            build_args=lambda cid: render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_campaign_rebuild_template,
                                id=cid,
                            ),
                        )

                    elif reb_prio_ring:
                        _fill_from_ring(
                            ring=reb_prio_ring,
                            ring_limit=spill,
                            total_limit=rebuild_total_limit,
                            root=root,
                            task_type="campaign_rebuild",
                            running=running,
                            ring_entities=reb_prio_set,
                            config=config,
                            store=store,
                            popens=popens,
                            build_args=lambda cid: render_mautic_command(
                                php_bin=config.php_bin,
                                run_as_user=config.mautic_run_as_user,
                                root=root,
                                template=config.cmd_campaign_rebuild_template,
                                id=cid,
                            ),
                        )

            if shared_campaign_cap > 0 and config.enable_campaign_rebuild and trigger_lane_configured:
                campaign_round_robin[root] = rr + 1

            if config.enable_import_polling and import_pending_cache.get(root, 0) > 0:
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_import_template,
                    import_limit=config.import_limit,
                )
                _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type="import",
                    entity_id=None,
                    args=args,
                    timeout_sec=config.command_timeout_sec,
                    max_parallel_for_type=1,
                    popens=popens,
                )

            last_cleanup = last_cleanup_ts.get(root, 0.0)
            interval = max(1, config.contacts_cleanup_interval_sec)
            if inst.mautic_timezone:
                try:
                    dt = datetime.now(ZoneInfo(inst.mautic_timezone))
                except Exception:
                    logging.warning("[%s] invalid mautic timezone in local.php: %s", root, inst.mautic_timezone)
                    dt = datetime.now()
            else:
                dt = datetime.now()
            in_quiet = dt.hour == max(0, min(23, config.contacts_cleanup_quiet_hour)) and dt.minute < max(
                1, min(180, config.contacts_cleanup_quiet_window_min)
            )
            if config.enable_contacts_cleanup and in_quiet and (last_cleanup == 0.0 or now - last_cleanup >= interval):
                try:
                    before = db.count_contacts_without_comm(
                        email_field=config.contacts_cleanup_email_field,
                        phone_field=config.contacts_cleanup_phone_field,
                        mode=config.contacts_cleanup_mode,
                    )
                    deleted = db.delete_contacts_without_comm(
                        email_field=config.contacts_cleanup_email_field,
                        phone_field=config.contacts_cleanup_phone_field,
                        mode=config.contacts_cleanup_mode,
                        max_delete=config.contacts_cleanup_max_delete_per_run,
                    )
                    logging.info("[%s] contacts_cleanup ok before=%s deleted=%s", root, before, deleted)
                except Exception as e:
                    logging.warning("[%s] contacts_cleanup failed: %s", root, e)
                last_cleanup_ts[root] = now

            last_cache_clear = last_cache_clear_ts.get(root, 0.0)
            if (
                config.enable_cache_clear
                and dt.hour == max(0, min(23, config.cache_clear_quiet_hour))
                and dt.minute < max(1, min(180, config.cache_clear_quiet_window_min))
                and (last_cache_clear == 0.0 or now - last_cache_clear >= max(1, config.cache_clear_interval_sec))
            ):
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_cache_clear_template,
                )
                if _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type="cache_clear",
                    entity_id=None,
                    args=args,
                    timeout_sec=config.command_timeout_sec,
                    max_parallel_for_type=1,
                    popens=popens,
                ):
                    last_cache_clear_ts[root] = now

            last_cache_warm = last_cache_warm_ts.get(root, 0.0)
            if (
                config.enable_cache_warm
                and dt.hour == max(0, min(23, config.cache_warm_quiet_hour))
                and dt.minute < max(1, min(180, config.cache_warm_quiet_window_min))
                and (last_cache_warm == 0.0 or now - last_cache_warm >= max(1, config.cache_warm_interval_sec))
            ):
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=config.cmd_cache_warm_template,
                )
                if _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type="cache_warm",
                    entity_id=None,
                    args=args,
                    timeout_sec=config.command_timeout_sec,
                    max_parallel_for_type=1,
                    popens=popens,
                ):
                    last_cache_warm_ts[root] = now

            for job in config.scheduled_jobs:
                if not job.enabled:
                    continue
                prev = jobs_last_run.get((root, job.name), 0.0)
                if prev > 0 and time.time() - prev < max(1, job.interval_sec):
                    continue
                if job.quiet_hour is not None:
                    if inst.mautic_timezone:
                        try:
                            dt_job = datetime.now(ZoneInfo(inst.mautic_timezone))
                        except Exception:
                            logging.warning("[%s] invalid mautic timezone in local.php: %s", root, inst.mautic_timezone)
                            dt_job = datetime.now()
                    else:
                        dt_job = datetime.now()
                    if not (
                        dt_job.hour == max(0, min(23, int(job.quiet_hour)))
                        and dt_job.minute < max(1, min(180, int(job.quiet_window_min)))
                    ):
                        continue
                args = render_mautic_command(
                    php_bin=config.php_bin,
                    run_as_user=config.mautic_run_as_user,
                    root=root,
                    template=job.command_template,
                    batch_limit=config.segment_batch_limit,
                    campaign_limit=config.campaign_limit,
                    import_limit=config.import_limit,
                )
                if _submit_if_slot(
                    config=config,
                    store=store,
                    running=running,
                    root=root,
                    task_type=f"job:{job.name}",
                    entity_id=None,
                    args=args,
                    timeout_sec=job.timeout_sec if job.timeout_sec else config.command_timeout_sec,
                    max_parallel_for_type=max(1, config.jobs_max_workers),
                    popens=popens,
                ):
                    jobs_last_run[(root, job.name)] = time.time()

        if single_cycle:
            return
        time.sleep(max(0.1, float(config.dispatch_interval_sec)))
