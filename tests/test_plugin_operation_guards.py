from datetime import datetime
from types import SimpleNamespace

from mcd_agent import daemon


class _DB:
    def __init__(self, count: int) -> None:
        self.count = count
        self.query = ""
        self.context = {}

    def fetch_count(self, query: str, context=None) -> int:
        self.query = query
        self.context = dict(context or {})
        return self.count


class _Store:
    def __init__(self) -> None:
        self.state = {}

    def get_runtime_sync(self, key: str):
        return self.state.get(key)

    def put_runtime_sync(self, key: str, value: dict) -> None:
        self.state[key] = dict(value)


def test_catalog_db_guard_uses_declared_query_and_instance_local_time() -> None:
    db = _DB(2)
    now = datetime(2026, 8, 14, 12, 34, 56)
    guard = {
        "type": "mautic_db_count",
        "query": "SELECT COUNT(*) FROM {prefix}plugin_rows WHERE seen_at <= '{now_local}'",
        "context": {"now_local": "instance.now_local"},
        "positive": True,
    }

    assert daemon._plugin_operation_guard_matches(db, guard, now) is True
    assert db.context == {"now_local": "2026-08-14 12:34:56"}
    assert "plugin_rows" in db.query


def test_bootstrap_state_and_task_marker_are_generic() -> None:
    store = _Store()
    digest = daemon._plugin_operation_bootstrap_digest("/srv/mautic", "vendor:sync", "initial")

    assert daemon._plugin_operation_bootstrap_completed(store, digest) is False
    daemon._mark_plugin_operation_bootstrap_completed(store, digest, reason="successful_bootstrap_command")
    assert daemon._plugin_operation_bootstrap_completed(store, digest) is True
    task = SimpleNamespace(task_type=f"job:plugin_operation_bootstrap:{digest}")
    assert daemon._plugin_operation_bootstrap_digest_from_task(task) == digest


def test_marker_only_bootstrap_stays_completed_without_a_db_guard() -> None:
    store = _Store()
    digest = daemon._plugin_operation_bootstrap_digest("/srv/mautic", "viber:stats_update", "stats_update")

    daemon._mark_plugin_operation_bootstrap_completed(store, digest, reason="successful_bootstrap_command")

    # The scheduler must keep this completion state when the catalog omits
    # complete_when. This is used for a one-time repair of already-present data.
    assert daemon._plugin_operation_bootstrap_is_completed(
        store,
        digest,
        db=_DB(0),
        complete_when=None,
        now_local=datetime(2026, 8, 31, 12, 0, 0),
    ) is True


def test_reset_on_enable_marks_a_new_plugin_operation_bootstrap_as_pending() -> None:
    store = _Store()
    digest = daemon._plugin_operation_bootstrap_digest("/srv/mautic", "viber:stats_update", "stats_update")
    now = datetime(2026, 8, 31, 12, 0, 0)

    daemon._sync_plugin_operation_bootstrap_enabled_state(store, digest, enabled=True)
    daemon._mark_plugin_operation_bootstrap_completed(store, digest, reason="successful_bootstrap_command")
    assert daemon._plugin_operation_bootstrap_is_completed(
        store, digest, db=_DB(0), complete_when=None, now_local=now
    ) is True

    daemon._sync_plugin_operation_bootstrap_enabled_state(store, digest, enabled=False)
    daemon._sync_plugin_operation_bootstrap_enabled_state(store, digest, enabled=True)
    assert daemon._plugin_operation_bootstrap_is_completed(
        store, digest, db=_DB(0), complete_when=None, now_local=now
    ) is False
