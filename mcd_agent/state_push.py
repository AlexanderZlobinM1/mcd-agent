from __future__ import annotations

from dataclasses import asdict
from datetime import timezone, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any
import uuid
from urllib import request
from urllib.error import URLError, HTTPError

from mcd_agent import __version__
from mcd_agent.apt_profile import collect_apt_state
from mcd_agent.backup import backup_profile_for_push, backup_state_for_push
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity
from mcd_agent.install_type import detect_install_type
from mcd_agent.inventory import InstanceInventory, MauticInstall, ensure_seeded
from mcd_agent.runtime_overrides import local_runtime_overrides
from mcd_agent.state_backend import (
    mark_outbound_event_mysql,
    mark_state_snapshot_push_result_mysql,
    prune_outbound_events_mysql,
    queue_outbound_event_mysql,
    read_pending_outbound_event_mysql,
    state_backend_status,
    upsert_state_snapshot_mysql,
)

_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*Bundle$")
_SEMVER_RE = re.compile(r"(\d+\.\d+\.\d+)")


def _is_valid_bundle_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if n.lower() in {"plugin", "plugins"}:
        return False
    return bool(_BUNDLE_NAME_RE.match(n))


def _hash_payload(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read_config_text(path: str) -> str | None:
    try:
        p = Path(path)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def _legacy_profile_event_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "profile-event.pending.json"


def _state_conn(cfg: AgentConfig) -> sqlite3.Connection:
    Path(cfg.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.state_db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          try_count INTEGER NOT NULL DEFAULT 0,
          last_try_at REAL,
          created_at REAL NOT NULL,
          sent_at REAL,
          last_error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_events_status ON outbound_events(status, created_at)")
    _migrate_legacy_profile_event_file(conn, cfg)
    return conn


def _migrate_legacy_profile_event_file(conn: sqlite3.Connection, cfg: AgentConfig) -> None:
    p = _legacy_profile_event_path(cfg)
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            p.unlink(missing_ok=True)
            return
    except Exception:
        try:
            p.unlink()
        except Exception:
            pass
        return
    event_id = str(raw.get("event_id", "")).strip() or uuid.uuid4().hex
    payload = dict(raw)
    payload["event_id"] = event_id
    now_ts = time.time()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_events
            (event_id, event_type, payload_json, status, try_count, created_at)
            VALUES (?, 'profile_event', ?, 'pending', 0, ?)
            """,
            (event_id, json.dumps(payload, ensure_ascii=True, separators=(",", ":")), now_ts),
        )
        conn.commit()
    finally:
        try:
            p.unlink()
        except Exception:
            pass


def queue_profile_event(
    cfg: AgentConfig,
    *,
    source: str,
    initiated_by_user: bool,
    old_profile: str | None,
    new_profile: str | None,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now_ts = time.time()
    payload: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "source": (source or "mcd_cli"),
        "initiated_by_user": bool(initiated_by_user),
        "old_profile": (old_profile or "").strip() or None,
        "new_profile": (new_profile or "").strip() or None,
        "reason": (reason or "profile_set"),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if isinstance(details, dict) and details:
        payload["details"] = details
    event_id = str(payload.get("event_id", "")).strip()
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    mysql_ok, mysql_msg = queue_outbound_event_mysql(
        cfg,
        event_type="profile_event",
        event_id=event_id,
        payload_json=payload_json,
        created_at=now_ts,
    )
    if not mysql_ok and mysql_msg not in {"mysql_state_disabled"}:
        logging.warning("outbound_events mysql queue failed, fallback to sqlite: %s", mysql_msg)
    if not mysql_ok:
        conn = _state_conn(cfg)
        try:
            conn.execute(
                """
                INSERT INTO outbound_events
                (event_id, event_type, payload_json, status, try_count, created_at)
                VALUES (?, 'profile_event', ?, 'pending', 0, ?)
                """,
                (
                    event_id,
                    payload_json,
                    now_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return payload


def read_pending_profile_event(cfg: AgentConfig) -> dict[str, Any] | None:
    raw_mysql, mysql_msg = read_pending_outbound_event_mysql(cfg, event_type="profile_event")
    if isinstance(raw_mysql, dict):
        return raw_mysql
    if mysql_msg not in {"mysql_state_disabled", "empty"}:
        logging.warning("outbound_events mysql read failed, fallback to sqlite: %s", mysql_msg)

    conn = _state_conn(cfg)
    try:
        row = conn.execute(
            """
            SELECT event_id, payload_json
            FROM outbound_events
            WHERE event_type='profile_event' AND status IN ('pending','failed')
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row["payload_json"] or "")
        raw = json.loads(payload_json)
        if isinstance(raw, dict):
            return raw
        # Invalid payload in queue: mark failed and keep diagnostics.
        conn.execute(
            """
            UPDATE outbound_events
            SET status='failed', try_count=try_count+1, last_try_at=?, last_error=?
            WHERE event_id=?
            """,
            (time.time(), "invalid_payload_type", str(row["event_id"] or "")),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    return None


def clear_pending_profile_event(
    cfg: AgentConfig,
    *,
    event_id: str | None = None,
    delivered: bool = True,
    error: str | None = None,
) -> None:
    target_id = (event_id or "").strip()
    if not target_id:
        return
    mysql_ok, mysql_msg = mark_outbound_event_mysql(
        cfg,
        event_id=target_id,
        delivered=delivered,
        error=error,
    )
    if not mysql_ok and mysql_msg not in {"mysql_state_disabled"}:
        logging.warning("outbound_events mysql mark failed, fallback sqlite only: %s", mysql_msg)

    conn = _state_conn(cfg)
    now_ts = time.time()
    try:
        if delivered:
            conn.execute(
                """
                UPDATE outbound_events
                SET status='sent', sent_at=?, last_try_at=?, last_error=NULL
                WHERE event_id=?
                """,
                (now_ts, now_ts, target_id),
            )
        else:
            conn.execute(
                """
                UPDATE outbound_events
                SET status='failed', try_count=try_count+1, last_try_at=?, last_error=?
                WHERE event_id=?
                """,
                (now_ts, (error or "delivery_failed")[:2000], target_id),
            )
        conn.commit()
    finally:
        conn.close()


def prune_sent_profile_events(cfg: AgentConfig, *, keep_days: int = 14) -> int:
    keep_sec = max(1, int(keep_days)) * 86400
    cutoff = time.time() - keep_sec
    mysql_deleted, mysql_msg = prune_outbound_events_mysql(
        cfg,
        event_type="profile_event",
        cutoff_ts=cutoff,
    )
    if mysql_msg not in {"ok", "mysql_state_disabled"}:
        logging.warning("outbound_events mysql prune failed, sqlite only: %s", mysql_msg)

    conn = _state_conn(cfg)
    try:
        cur = conn.execute(
            "DELETE FROM outbound_events WHERE event_type='profile_event' AND status='sent' AND COALESCE(sent_at, created_at) < ?",
            (cutoff,),
        )
        conn.commit()
        return int(mysql_deleted or 0) + int(cur.rowcount or 0)
    finally:
        conn.close()


def _extract_version_from_php_text(text: str) -> str:
    m = re.search(r"['\"]version['\"]\s*=>\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"['\"]version['\"]\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "-"


def _candidate_roots(root: str) -> list[Path]:
    root_path = Path(root)
    candidates: list[Path] = [root_path]
    base = root_path.name.lower()
    if base in {"public", "docroot", "public_html"}:
        candidates.append(root_path.parent)
    candidates.append(root_path.parent.parent)
    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        ps = str(p)
        if ps == "/" or ps in seen:
            continue
        seen.add(ps)
        out.append(p)
    return out


def _read_version_from_console(root: Path, php_bin: str) -> str | None:
    console = root / "bin" / "console"
    if not console.exists():
        return None
    cmds = [
        [php_bin, str(console), "--version"],
        [php_bin, str(console), "about", "--no-interaction"],
    ]
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=30)
        except Exception:
            continue
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = _SEMVER_RE.search(out)
        if m:
            return m.group(1)
    return None


def _read_version_from_composer_lock(root: Path) -> str | None:
    lock = root / "composer.lock"
    if not lock.exists():
        return None
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return None
    packages = data.get("packages", [])
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        if str(pkg.get("name", "")) not in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
            continue
        v = str(pkg.get("version", ""))
        m = _SEMVER_RE.search(v)
        if m:
            return m.group(1)
    return None


def _collect_mautic_version(root: str, php_bin: str) -> str:
    for candidate in _candidate_roots(root):
        v = _read_version_from_console(candidate, php_bin)
        if v:
            return v
        v = _read_version_from_composer_lock(candidate)
        if v:
            return v
    return "-"


def _collect_installed_plugins(root: str, limit: int = 200) -> list[dict[str, str]]:
    base = Path(root)
    candidates = [
        base / "plugins",
        base / "docroot" / "plugins",
        base / "public" / "plugins",
    ]
    plugins_dir = None
    for p in candidates:
        if p.exists() and p.is_dir():
            plugins_dir = p
            break
    if plugins_dir is None:
        return []
    rows: list[dict[str, str]] = []
    for name in sorted(os.listdir(plugins_dir)):
        if name.startswith("."):
            continue
        if not _is_valid_bundle_name(name):
            continue
        pdir = plugins_dir / name
        if not pdir.is_dir():
            continue
        cfg = pdir / "Config" / "config.php"
        ver = "-"
        if cfg.exists():
            try:
                ver = _extract_version_from_php_text(cfg.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                ver = "-"
        rows.append({"bundle": name, "version": ver or "-"})
        if len(rows) >= limit:
            break
    return rows


class MCCStatePusher:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self.last_push_ts = 0.0
        self.last_alert_poll_ts = 0.0
        self.last_hash = ""
        self.last_alert_level = 0
        self.latest_signals: dict[str, Any] | None = None
        self.latest_signals_ts = 0.0
        self.latest_apt_state: dict[str, Any] | None = None
        self.latest_apt_state_ts = 0.0
        self.latest_apt_probe_key = ""

    def enabled(self) -> bool:
        return bool(self.cfg.mcc_push_enabled and self.cfg.mcc_url and self.cfg.mcc_token)

    def set_signals(self, payload: dict[str, Any], now_ts: float) -> None:
        self.latest_signals = payload
        self.latest_signals_ts = now_ts

    def _apt_probe_key(self) -> str:
        """
        Lightweight fingerprint for local APT state.
        If package DB/sources changed, refresh apt_state immediately
        even when cache interval is not yet elapsed.
        """
        paths = [
            Path("/var/lib/dpkg/status"),
            Path("/var/lib/apt/periodic/update-success-stamp"),
            Path("/var/lib/apt/lists"),
            Path("/etc/apt/sources.list"),
            Path("/etc/apt/sources.list.d"),
        ]
        parts: list[str] = []
        for p in paths:
            try:
                st = p.stat()
                parts.append(f"{p}:{int(st.st_mtime_ns)}:{int(st.st_size)}")
            except Exception:
                parts.append(f"{p}:missing")
        return "|".join(parts)

    def _apt_state(self, now_ts: float) -> dict[str, Any]:
        interval = max(30, int(getattr(self.cfg, "mcc_push_apt_state_interval_sec", 120) or 120))
        probe_key = self._apt_probe_key()
        if (
            self.latest_apt_state
            and self.latest_apt_probe_key == probe_key
            and (now_ts - self.latest_apt_state_ts) < interval
        ):
            return dict(self.latest_apt_state)
        payload = collect_apt_state(timeout_sec=30)
        self.latest_apt_state = payload
        self.latest_apt_state_ts = now_ts
        self.latest_apt_probe_key = probe_key
        return dict(payload)

    def should_push(self, now_ts: float, payload_no_ts: dict[str, Any]) -> tuple[bool, bool]:
        force_alert = False
        new_hash = _hash_payload(payload_no_ts)
        changed = new_hash != self.last_hash

        if self.latest_signals:
            lvl = 0
            overall = self.latest_signals.get("overall")
            if isinstance(overall, dict):
                try:
                    lvl = int(overall.get("level", 0) or 0)
                except Exception:
                    lvl = 0
            if lvl > 0 and lvl != self.last_alert_level:
                force_alert = True
            self.last_alert_level = lvl

        interval_due = (now_ts - self.last_push_ts) >= max(1, int(self.cfg.mcc_push_interval_sec))
        change_due = bool(self.cfg.mcc_push_on_change and changed)
        return (interval_due or change_due or force_alert), changed

    def mark_pushed(self, now_ts: float, payload_no_ts: dict[str, Any]) -> None:
        self.last_push_ts = now_ts
        self.last_hash = _hash_payload(payload_no_ts)

    def _store_state_snapshot(self, payload: dict[str, Any], now_ts: float) -> None:
        payload_hash = _hash_payload(payload)
        ok, msg = upsert_state_snapshot_mysql(
            self.cfg,
            payload=payload,
            payload_hash=payload_hash,
            created_at=now_ts,
        )
        if not ok and msg not in {"mysql_state_disabled", "snapshot_disabled"}:
            logging.warning("state snapshot mysql upsert failed: %s", msg)

    def build_payload(
        self,
        *,
        installs: list[MauticInstall],
        profile_name: str,
        now_ts: float,
        include_signals: bool = True,
        profile_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = resolve_agent_identity(self.cfg)
        instances = []
        for i in installs:
            instances.append(
                {
                    "instance_uid": i.instance_uid,
                    "name": i.name,
                    "root": i.root,
                    "source": i.source,
                    "mautic_major": i.mautic_major,
                    "mautic_version": _collect_mautic_version(i.root, self.cfg.php_bin),
                    "install_type": detect_install_type(i.root),
                    "plugins": _collect_installed_plugins(i.root),
                }
            )
        instances.sort(key=lambda x: str(x["instance_uid"]))

        payload: dict[str, Any] = {
            "schema": "mcd-state-v1",
            "agent_version": __version__,
            "hostname": str(identity.get("effective_hostname") or ""),
            "mcc_host_name": str(identity.get("effective_mcc_host_name") or ""),
            "agent_hostname": str(identity.get("local_hostname") or ""),
            "configured_host_name": str(identity.get("configured_host_name") or ""),
            "template_state": {
                "is_template": bool(identity.get("is_template", False)),
                "autopromote_on_clone": bool(identity.get("autopromote_on_clone", True)),
                "clone_detected": bool(identity.get("clone_detected", False)),
                "source_host_name": str(identity.get("source_host_name") or ""),
            },
            "profile": (profile_name or "").strip().lower(),
            "runtime_overrides": local_runtime_overrides(self.cfg),
            "config_state": {
                "path": self.cfg.config_file_path,
                "schema_version": int(self.cfg.config_schema_version),
                "customized": bool(self.cfg.config_customized),
                "sha256": self.cfg.config_sha256,
                "toml": _read_config_text(self.cfg.config_file_path),
            },
            "update_state": {
                "policy": self.cfg.mcd_update_policy,
                "auto_update_enabled": bool(self.cfg.mcd_auto_update_enabled),
                "allow_test_build": bool(self.cfg.mcd_update_allow_test_build),
            },
            "backup_state": backup_state_for_push(self.cfg),
            "backup_profile": backup_profile_for_push(self.cfg),
            "apt_state": self._apt_state(now_ts),
            "state_backend": state_backend_status(self.cfg, probe=True),
            "instances": instances,
            "sent_at_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if include_signals:
            payload["signals"] = self.latest_signals or {}
            payload["signals_collected_at_ts"] = int(self.latest_signals_ts or 0)
        if isinstance(profile_event, dict) and profile_event:
            payload["profile_event"] = profile_event
        self._store_state_snapshot(payload, now_ts)
        return payload

    def send(self, payload: dict[str, Any], timeout_sec: int = 5) -> tuple[bool, str]:
        if not self.enabled():
            return False, "push disabled"
        payload_hash = _hash_payload(payload)
        base = str(self.cfg.mcc_url).rstrip("/")
        url = base + "/api/v1/agent/state"
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = request.Request(
            url=url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.mcc_token}",
                "X-MCD-Agent-Version": __version__,
            },
        )
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                code = int(getattr(resp, "status", 200))
                body = (resp.read() or b"").decode("utf-8", errors="replace")
                if 200 <= code < 300:
                    mark_state_snapshot_push_result_mysql(
                        self.cfg,
                        payload_hash=payload_hash,
                        delivered=True,
                        message=body.strip() or "ok",
                    )
                    return True, body.strip() or "ok"
                mark_state_snapshot_push_result_mysql(
                    self.cfg,
                    payload_hash=payload_hash,
                    delivered=False,
                    message=f"http {code}: {body.strip()}",
                )
                return False, f"http {code}: {body.strip()}"
        except HTTPError as e:
            try:
                msg = (e.read() or b"").decode("utf-8", errors="replace").strip()
            except Exception:
                msg = ""
            mark_state_snapshot_push_result_mysql(
                self.cfg,
                payload_hash=payload_hash,
                delivered=False,
                message=f"http {e.code}: {msg or e.reason}",
            )
            return False, f"http {e.code}: {msg or e.reason}"
        except URLError as e:
            mark_state_snapshot_push_result_mysql(
                self.cfg,
                payload_hash=payload_hash,
                delivered=False,
                message=f"urlerror: {e.reason}",
            )
            return False, f"urlerror: {e.reason}"
        except Exception as e:
            mark_state_snapshot_push_result_mysql(
                self.cfg,
                payload_hash=payload_hash,
                delivered=False,
                message=str(e),
            )
            return False, str(e)


def safe_signals_level(payload: dict[str, Any] | None) -> int:
    if not payload:
        return 0
    try:
        overall = payload.get("overall")
        if isinstance(overall, dict):
            return int(overall.get("level", 0) or 0)
    except Exception:
        return 0
    return 0


def should_poll_alert(now_ts: float, last_poll_ts: float, interval_sec: int) -> bool:
    return (now_ts - last_poll_ts) >= max(1, int(interval_sec))


def log_push_result(ok: bool, msg: str) -> None:
    if ok:
        logging.info("MCC push ok: %s", msg)
    else:
        logging.warning("MCC push failed: %s", msg)


def push_state_now(
    cfg: AgentConfig,
    *,
    profile_name: str | None = None,
    include_signals: bool = False,
) -> tuple[bool, str]:
    pusher = MCCStatePusher(cfg)
    if not pusher.enabled():
        return False, "push disabled"

    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    installs = inv.list_instances()
    now_ts = datetime.now(timezone.utc).timestamp()
    profile_event = read_pending_profile_event(cfg)
    payload = pusher.build_payload(
        installs=installs,
        profile_name=(profile_name if profile_name is not None else cfg.profile_name),
        now_ts=now_ts,
        include_signals=include_signals,
        profile_event=profile_event,
    )
    ok, msg = pusher.send(payload)
    if profile_event:
        clear_pending_profile_event(
            cfg,
            event_id=str(profile_event.get("event_id", "")).strip() or None,
            delivered=bool(ok),
            error=None if ok else str(msg),
        )
    return ok, msg
