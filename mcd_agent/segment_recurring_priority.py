from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RUNTIME_KEY = "segment_recurring_priority_v1"
STATE_SCHEMA = "mcd-segment-recurring-priority-v1"
MIN_INTERVAL_SEC = 10


@dataclass(frozen=True)
class RecurringPrioritySegment:
    segment_id: int
    max_interval_sec: int


def _instance_keys(inst: object) -> list[str]:
    values = [
        getattr(inst, "instance_uid", None),
        getattr(inst, "root", None),
        getattr(inst, "name", None),
        getattr(inst, "primary_domain", None),
    ]
    domains = getattr(inst, "domains", None)
    if isinstance(domains, list):
        values.extend(domains)
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def entries_for_instance(settings: object, inst: object) -> list[RecurringPrioritySegment]:
    """Return explicit recurring segment entries for one instance; no default scope exists."""
    if not isinstance(settings, dict):
        return []
    raw: object = None
    for key in _instance_keys(inst):
        if key in settings:
            raw = settings[key]
            break
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
        return []

    intervals: dict[int, int] = {}
    for item in raw["segments"]:
        if not isinstance(item, dict):
            continue
        try:
            segment_id = int(item.get("id") or 0)
            interval_sec = int(item.get("max_interval_sec") or 0)
        except (TypeError, ValueError):
            continue
        if segment_id <= 0 or interval_sec < MIN_INTERVAL_SEC:
            continue
        intervals[segment_id] = min(interval_sec, intervals.get(segment_id, interval_sec))
    return [
        RecurringPrioritySegment(segment_id=segment_id, max_interval_sec=intervals[segment_id])
        for segment_id in sorted(intervals)
    ]


def state_key(root: str, segment_id: int) -> str:
    return f"segment_recurring_priority:{root}:{int(segment_id)}"


def state_payload(
    *,
    root: str,
    instance_uid: str,
    segment_id: int,
    max_interval_sec: int,
    active: bool,
    pid: int | None,
    last_started_at: float | None,
    last_finished_at: float | None,
    last_status: str,
    last_rc: int | None,
    last_error: str,
    next_run_at: float,
    updated_at: float,
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "root": str(root),
        "instance_uid": str(instance_uid),
        "segment_id": int(segment_id),
        "max_interval_sec": int(max_interval_sec),
        "active": bool(active),
        "pid": int(pid) if pid is not None else None,
        "last_started_at": float(last_started_at) if last_started_at is not None else None,
        "last_finished_at": float(last_finished_at) if last_finished_at is not None else None,
        "last_status": str(last_status),
        "last_rc": int(last_rc) if last_rc is not None else None,
        "last_error": str(last_error or "")[:500],
        "next_run_at": float(next_run_at),
        "updated_at": float(updated_at),
    }
