from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = CONTROL_PLANE_ROOT / "local-mcc" / "mauticctl" / "custom" / "scripts" / "campaign-pool-worker.py"


def _load_module():
    if not (CONTROL_PLANE_ROOT / "agent").exists() or not SCRIPT_PATH.exists():
        pytest.skip("campaign pool worker tests require a full control-plane checkout")
    spec = spec_from_file_location("campaign_pool_worker", SCRIPT_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_trigger_backlog_sql_tracks_root_contacts_and_due_events():
    module = _load_module()

    sql = module._trigger_backlog_sql(5129)

    assert "campaign_leads cl" in sql
    assert "campaign_events ev" in sql
    assert "ev.parent_id IS NULL" in sql
    assert "date_last_exited IS NULL" in sql
    assert "el.is_scheduled = 1" in sql
    assert "el.date_triggered IS NULL" in sql
    assert "el.date_triggered < el.trigger_date" in sql
    assert "trigger_date <= NOW()" in sql
    assert "trigger_date <= UTC_TIMESTAMP()" in sql
    assert "MAX(el.id)" in sql


def test_trigger_backlog_progress_counts_root_and_due_rows():
    module = _load_module()

    backlog = {
        "root_contacts": 244350,
        "due_events": 42,
        "remaining": 244392,
        "marker": 9001,
    }

    assert not module.trigger_backlog_drained(backlog)
    assert module.format_trigger_backlog(backlog) == (
        "root_contacts=244350 due_events=42 remaining=244392 marker=9001"
    )


def test_trigger_backlog_progress_detects_root_drain_even_if_due_grows():
    module = _load_module()

    previous = {"root_contacts": 1000, "due_events": 20, "remaining": 1020}
    current = {"root_contacts": 900, "due_events": 25, "remaining": 925}

    assert module.trigger_backlog_progressed(current, previous)


def test_trigger_backlog_drained_requires_root_zero_and_small_due_tail():
    module = _load_module()

    assert module.trigger_backlog_drained(
        {"root_contacts": 0, "due_events": 5, "remaining": 5}
    )
    assert not module.trigger_backlog_drained(
        {"root_contacts": 1, "due_events": 0, "remaining": 1}
    )
    assert not module.trigger_backlog_drained(
        {"root_contacts": 0, "due_events": 6, "remaining": 6}
    )


def test_campaign_sets_use_finite_deadline_priority_and_exclude_viber(monkeypatch):
    module = _load_module()
    responses = iter(
        [
            [["9"]],
            [["100", "0"], ["99", "9"], ["98", "0"], ["97", "0"]],
        ]
    )
    queries = []

    def fake_mysql_rows(sql):
        queries.append(sql)
        return next(responses)

    monkeypatch.setattr(module, "mysql_rows", fake_mysql_rows)

    pools = module.get_campaign_sets()

    assert "ORDER BY (publish_down IS NULL) ASC, publish_down ASC, id DESC" in queries[1]
    assert pools["elastic-rebuild"] == [100, 98, 97]
    assert pools["reserve-trigger"] == [100, 98, 97]
    assert pools["hot-trigger"] == [100, 98, 97]
    assert pools["rest-trigger"] == []


def test_trigger_claims_assign_distinct_campaigns_to_distinct_slots(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    monkeypatch.setattr(module, "campaign_command_active", lambda *_args: False)
    monkeypatch.setattr(module, "lock_nonblocking", lambda path: path)
    state = {}
    ids = [30, 20, 10]

    first = module.pick_and_claim_trigger(state, "hot-trigger:w1", ids, 0, ids)
    second = module.pick_and_claim_trigger(state, "reserve-trigger:w1", ids, 0, ids)

    assert first[:3] == (30, 0, 1)
    assert second[:3] == (20, 1, 2)
    assert state[module.TRIGGER_CLAIMS_KEY]["hot-trigger:w1"]["cid"] == 30
    assert state[module.TRIGGER_CLAIMS_KEY]["reserve-trigger:w1"]["cid"] == 20


def test_trigger_claim_keeps_unfinished_campaign_sticky(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    monkeypatch.setattr(module, "campaign_command_active", lambda *_args: False)
    monkeypatch.setattr(module, "lock_nonblocking", lambda path: path)
    owner = "hot-trigger:w1"
    state = {
        module.TRIGGER_CLAIMS_KEY: {
            owner: {"cid": 20, "ts": 950, "marker": 42, "stalled": 0, "failures": 0}
        }
    }

    selected = module.pick_and_claim_trigger(state, owner, [30, 20, 10], 0, [30, 20, 10])

    assert selected[:3] == (20, 1, 2)
    assert state[module.TRIGGER_CLAIMS_KEY][owner]["cid"] == 20
    assert state[module.TRIGGER_CLAIMS_KEY][owner]["ts"] == 1000


def test_trigger_claim_skips_campaign_in_cooldown(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    monkeypatch.setattr(module, "campaign_command_active", lambda *_args: False)
    monkeypatch.setattr(module, "lock_nonblocking", lambda path: path)
    state = {module.TRIGGER_COOLDOWNS_KEY: {"30": 1060}}

    selected = module.pick_and_claim_trigger(
        state,
        "reserve-trigger:w1",
        [30, 20, 10],
        0,
        [30, 20, 10],
    )

    assert selected[:3] == (20, 1, 2)


def test_trigger_pressure_uses_only_fresh_claims(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "time", lambda: 1000)

    assert module.trigger_pressure_active(
        {module.TRIGGER_CLAIMS_KEY: {"slot": {"cid": 30, "ts": 900}}}
    )
    assert not module.trigger_pressure_active(
        {module.TRIGGER_CLAIMS_KEY: {"slot": {"cid": 30, "ts": 800}}}
    )
