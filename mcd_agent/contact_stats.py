from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from mcd_agent.db import MauticDB
from mcd_agent.models import MauticInstall


CACHE_SCHEMA = "mcd-contact-stats-v1"
DEFAULT_REFRESH_INTERVAL_SEC = 3600
CONTACT_STATS_CACHE_NAME = "contact-stats-cache.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _cache_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / CONTACT_STATS_CACHE_NAME


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": CACHE_SCHEMA, "instances": {}}
    if not isinstance(parsed, dict):
        return {"schema": CACHE_SCHEMA, "instances": {}}
    instances = parsed.get("instances")
    if not isinstance(instances, dict):
        parsed["instances"] = {}
    parsed["schema"] = CACHE_SCHEMA
    return parsed


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _is_fresh(item: dict[str, Any] | None, now_ts: float, refresh_interval_sec: int | None) -> bool:
    if not isinstance(item, dict):
        return False
    measured = _safe_int(item.get("measured_at_ts"))
    if measured is None:
        return False
    interval = DEFAULT_REFRESH_INTERVAL_SEC if refresh_interval_sec is None else int(refresh_interval_sec)
    if interval <= 0:
        return False
    return now_ts - float(measured) < max(60, interval)


def _table_exists(db: MauticDB, table: str) -> bool:
    rows = db.fetch_rows(
        """
        SELECT 1 AS present
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = '{table_name}'
        LIMIT 1
        """,
        limit=1,
        context={"table_name": table.replace("'", "''")},
    )
    return bool(rows)


def _measure_instance_contact_stats(inst: MauticInstall) -> dict[str, Any]:
    if not inst.db:
        return {
            "instance_uid": inst.instance_uid,
            "name": inst.name,
            "root": inst.root,
            "status": "error",
            "error": "db_config_missing",
            "measured_at_utc": _utc_now(),
            "measured_at_ts": int(time.time()),
        }
    db = MauticDB(inst.db)
    prefix = str(inst.db.table_prefix or "")
    leads_table = db._safe_table(f"{prefix}leads")
    dnc_table = db._safe_table(f"{prefix}lead_donotcontact")
    measured_at_ts = int(time.time())
    measured_at_utc = _utc_now()
    if not _table_exists(db, leads_table):
        return {
            "instance_uid": inst.instance_uid,
            "name": inst.name,
            "root": inst.root,
            "status": "error",
            "error": f"table_missing:{leads_table}",
            "measured_at_utc": measured_at_utc,
            "measured_at_ts": measured_at_ts,
        }
    total = db.fetch_count(f"SELECT COUNT(*) AS cnt FROM `{leads_table}`")
    dnc = 0
    if _table_exists(db, dnc_table):
        dnc = db.fetch_count(
            f"""
            SELECT COUNT(DISTINCT dnc.lead_id) AS cnt
            FROM `{dnc_table}` dnc
            INNER JOIN `{leads_table}` l ON l.id = dnc.lead_id
            """
        )
    return {
        "instance_uid": inst.instance_uid,
        "name": inst.name,
        "root": inst.root,
        "status": "ok",
        "total_contacts": int(total),
        "dnc_contacts": int(dnc),
        "measured_at_utc": measured_at_utc,
        "measured_at_ts": measured_at_ts,
    }


def collect_contact_stats(
    installs: list[MauticInstall],
    *,
    state_dir: str | Path,
    refresh_interval_sec: int = DEFAULT_REFRESH_INTERVAL_SEC,
) -> list[dict[str, Any]]:
    path = _cache_path(state_dir)
    cache = _read_cache(path)
    cached_instances = cache.get("instances")
    if not isinstance(cached_instances, dict):
        cached_instances = {}
        cache["instances"] = cached_instances
    now_ts = time.time()
    changed = False
    out: list[dict[str, Any]] = []
    active_uids = {str(inst.instance_uid or "").strip() for inst in installs if str(inst.instance_uid or "").strip()}

    for inst in installs:
        uid = str(inst.instance_uid or "").strip()
        if not uid:
            continue
        cached = cached_instances.get(uid)
        if _is_fresh(cached if isinstance(cached, dict) else None, now_ts, refresh_interval_sec):
            item = dict(cached)
            item["cache_status"] = "cached"
            out.append(item)
            continue

        try:
            item = _measure_instance_contact_stats(inst)
            item["cache_status"] = "refreshed"
            cached_instances[uid] = dict(item)
            changed = True
            out.append(item)
        except Exception as exc:
            if isinstance(cached, dict) and cached:
                item = dict(cached)
                item["cache_status"] = "stale"
                item["last_error"] = str(exc)[:240]
                out.append(item)
            else:
                item = {
                    "instance_uid": uid,
                    "name": inst.name,
                    "root": inst.root,
                    "status": "error",
                    "cache_status": "error",
                    "error": str(exc)[:240],
                    "measured_at_utc": _utc_now(),
                    "measured_at_ts": int(time.time()),
                }
                cached_instances[uid] = dict(item)
                changed = True
                out.append(item)

    for uid in list(cached_instances.keys()):
        if uid not in active_uids:
            cached_instances.pop(uid, None)
            changed = True

    if changed:
        cache["updated_at_utc"] = _utc_now()
        _write_cache(path, cache)
    out.sort(key=lambda x: str(x.get("instance_uid") or ""))
    return out
