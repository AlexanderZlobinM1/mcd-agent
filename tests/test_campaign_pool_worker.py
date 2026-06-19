from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "local-mcc"
    / "mauticctl"
    / "custom"
    / "scripts"
    / "campaign-pool-worker.py"
)


def _load_module():
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
    assert "date_triggered IS NULL" in sql
    assert "trigger_date <= NOW()" in sql
    assert "trigger_date <= UTC_TIMESTAMP()" in sql


def test_trigger_backlog_progress_counts_root_and_due_rows():
    module = _load_module()

    backlog = {"root_contacts": 244350, "due_events": 42, "remaining": 244392}

    assert not module.trigger_backlog_drained(backlog)
    assert module.format_trigger_backlog(backlog) == (
        "root_contacts=244350 due_events=42 remaining=244392"
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


def test_viber_campaigns_are_routed_to_dedicated_rings(monkeypatch):
    module = _load_module()

    def fake_mysql_rows(sql):
        if "FROM ananas_categories" in sql:
            return [["359"]]
        if "FROM ananas_campaigns" in sql:
            return [
                ["5144", "359"],
                ["5143", "0"],
                ["5142", "0"],
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(module, "mysql_rows", fake_mysql_rows)

    pools = module.get_campaign_sets()

    assert pools["viber-trigger"] == [5144]
    assert pools["viber-rebuild"] == [5144]
    assert 5144 not in pools["latest-trigger"]
    assert 5144 not in pools["hot-trigger"]
    assert 5144 not in pools["rest-trigger"]
    assert 5144 not in pools["hot-rebuild"]
    assert 5144 not in pools["rest-rebuild"]
