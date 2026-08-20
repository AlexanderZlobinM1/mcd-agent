from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcd_agent.db import MauticDB


SUPPORTED_MAUTIC_MAJORS = frozenset({5, 6, 7})
DEFAULT_INTERVAL_SEC = 3600
MIN_INTERVAL_SEC = 60
MAX_INTERVAL_SEC = 86_400


def supports_message_queue(inst: object) -> bool:
    try:
        return int(getattr(inst, "mautic_major", 0) or 0) in SUPPORTED_MAUTIC_MAJORS
    except (TypeError, ValueError):
        return False


def _boolish(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def normalize_interval(value: object, default: int = DEFAULT_INTERVAL_SEC) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(MIN_INTERVAL_SEC, min(MAX_INTERVAL_SEC, parsed))


def instance_setting_keys(inst: object) -> list[str]:
    raw = [
        getattr(inst, "instance_uid", None),
        getattr(inst, "root", None),
        getattr(inst, "name", None),
        getattr(inst, "primary_domain", None),
    ]
    domains = getattr(inst, "domains", None)
    if isinstance(domains, list):
        raw.extend(domains)
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def effective_message_queue_setting(config: object, inst: object) -> tuple[bool, int]:
    enabled = _boolish(getattr(config, "message_queue_enabled", False), False)
    interval_sec = normalize_interval(
        getattr(config, "message_queue_interval_sec", DEFAULT_INTERVAL_SEC)
    )
    settings = getattr(config, "message_queue_instance_settings", {})
    if not isinstance(settings, dict):
        return enabled, interval_sec
    for key in [*instance_setting_keys(inst), "default"]:
        if key not in settings:
            continue
        raw = settings.get(key)
        if isinstance(raw, dict):
            enabled = _boolish(raw.get("enabled"), enabled)
            interval_sec = normalize_interval(raw.get("interval_sec"), interval_sec)
            return enabled, interval_sec
        if isinstance(raw, bool):
            return raw, interval_sec
        return enabled, normalize_interval(raw, interval_sec)
    return enabled, interval_sec


def collect_message_queue_snapshot(inst: object) -> dict[str, Any]:
    try:
        major = int(getattr(inst, "mautic_major", 0) or 0)
    except (TypeError, ValueError):
        major = 0
    snapshot: dict[str, Any] = {
        "supported": major in SUPPORTED_MAUTIC_MAJORS,
        "available": False,
        "total": 0,
        "due": 0,
        "exhausted": 0,
        "future": 0,
        "next_scheduled_at": None,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "error": "",
    }
    if not snapshot["supported"]:
        snapshot["error"] = "unsupported_mautic_major"
        return snapshot
    db_cfg = getattr(inst, "db", None)
    if db_cfg is None:
        snapshot["error"] = "database_config_unavailable"
        return snapshot
    try:
        observed = MauticDB(db_cfg).fetch_message_queue_snapshot()
        for key in ("total", "due", "exhausted", "future"):
            try:
                snapshot[key] = max(0, int(observed.get(key) or 0))
            except (TypeError, ValueError):
                snapshot[key] = 0
        snapshot["next_scheduled_at"] = (
            str(observed.get("next_scheduled_at") or "").strip() or None
        )
        snapshot["error"] = str(observed.get("error") or "").strip()[:160]
        snapshot["available"] = True
    except Exception as exc:
        snapshot["error"] = f"queue_probe_failed:{type(exc).__name__}"
    return snapshot
