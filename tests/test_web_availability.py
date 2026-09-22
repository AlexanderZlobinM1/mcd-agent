from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.web_availability import collect_web_availability


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(tmp_path / "state.db"),
        web_availability_endpoint="https://example.test/",
        web_availability_cooldown_sec=300,
    )


def test_web_availability_requires_three_failures_before_one_restart(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    restarts: list[str] = []
    probe = lambda _endpoint: {"status": "unavailable", "phase": "http", "diagnosis": "http_failure", "http_status": 503}
    restart = lambda service: (restarts.append(service) or (True, "success"))

    first = collect_web_availability(cfg, [], now=100, probe=probe, restart=restart)
    second = collect_web_availability(cfg, [], now=131, probe=probe, restart=restart)
    third = collect_web_availability(cfg, [], now=162, probe=probe, restart=restart)

    assert first is not None and first["remediation"]["decision"] == "none"
    assert second is not None and second["remediation"]["decision"] == "none"
    assert third is not None and third["remediation"]["decision"] == "completed"
    assert restarts == ["nginx"]


def test_web_availability_cooldown_and_two_attempt_limit(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    restarts: list[str] = []
    probe = lambda _endpoint: {"status": "unavailable", "phase": "http", "diagnosis": "http_failure", "http_status": 503}
    restart = lambda service: (restarts.append(service) or (False, "failed"))

    for now in (100, 131, 162, 500, 531, 562, 900, 931, 962):
        observation = collect_web_availability(cfg, [], now=now, probe=probe, restart=restart)

    assert observation is not None
    assert observation["remediation"]["decision"] == "manual_attention"
    assert len(restarts) == 2


def test_web_availability_success_resets_outage_state(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    failures = iter(
        [
            {"status": "unavailable", "phase": "http", "diagnosis": "http_failure", "http_status": 503},
            {"status": "available", "phase": "mautic", "diagnosis": "ok", "http_status": 200, "latency_ms": 8},
        ]
    )
    probe = lambda _endpoint: next(failures)
    collect_web_availability(cfg, [], now=100, probe=probe, restart=lambda _service: (True, "success"))
    healthy = collect_web_availability(cfg, [], now=131, probe=probe, restart=lambda _service: (True, "success"))

    assert healthy is not None
    assert healthy["status"] == "available"
    state = json.loads((tmp_path / "web-availability.json").read_text(encoding="utf-8"))
    assert state["host::https://example.test/"]["consecutive_failures"] == 0
