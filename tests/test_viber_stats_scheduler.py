from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import daemon
from mcd_agent.db import MauticDB
from mcd_agent.state_push import MCCStatePusher


class ViberStatsSchedulerTests(unittest.TestCase):
    def test_requires_mautic_registered_bundle_for_scheduled_plugin_operation(self) -> None:
        db = Mock()
        item = {"bundle": "SalesSnapViberBundle"}

        db.has_installed_plugin_matching.return_value = False
        self.assertFalse(daemon._plugin_operation_has_registered_bundle(db, item))
        db.has_installed_plugin_matching.assert_called_once_with("SalesSnapViberBundle")

        db.has_installed_plugin_matching.return_value = True
        self.assertTrue(daemon._plugin_operation_has_registered_bundle(db, item))

    def test_missing_registry_state_fails_closed(self) -> None:
        self.assertFalse(daemon._plugin_operation_has_registered_bundle(None, {"bundle": "SalesSnapViberBundle"}))
        self.assertFalse(daemon._plugin_operation_has_registered_bundle(Mock(), {}))

    def test_database_plugin_query_excludes_missing_bundle(self) -> None:
        class Cursor:
            def __init__(self, row: object) -> None:
                self.row = row
                self.query = ""
                self.params: tuple[object, ...] = ()

            def __enter__(self) -> "Cursor":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def execute(self, query: str, params: tuple[object, ...]) -> int:
                self.query, self.params = query, params
                return 1

            def fetchone(self) -> object:
                return self.row

        class Connection:
            def __init__(self, cursor: Cursor) -> None:
                self.cursor_value = cursor

            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def cursor(self) -> Cursor:
                return self.cursor_value

        cfg = SimpleNamespace(table_prefix="ss_")
        db = object.__new__(MauticDB)
        db.cfg = cfg
        cursor = Cursor({"1": 1})
        db._connect = lambda: Connection(cursor)  # type: ignore[method-assign]
        db._table_columns = lambda *_: {"bundle", "is_missing"}  # type: ignore[method-assign]

        self.assertTrue(db.has_installed_plugin_matching("viber"))
        self.assertIn("COALESCE(`is_missing`, 0) = 0", cursor.query)
        self.assertEqual(cursor.params, ("%viber%",))

    def test_packaged_scheduler_source_contains_viber_command(self) -> None:
        source = (Path(__file__).parents[1] / "mcd_agent" / "daemon.py").read_text(encoding="utf-8")
        self.assertIn("_plugin_operation_has_registered_bundle(db, plugin_item)", source)
        self.assertIn("plugin_operations_for_instance(config, inst)", source)

    def test_long_campaign_does_not_consume_recurring_plugin_operation_slot(self) -> None:
        cfg = SimpleNamespace(scheduler_host_max_parallel=1)
        campaign = daemon.RunningTask(
            row_id=1,
            root="/srv/mautic",
            task_key="campaign",
            task_type="campaign_trigger",
            entity_id=42,
            command_str="campaign",
            timeout_sec=0,
            attempts=1,
            started_at=1.0,
            pid=123,
        )
        running = {campaign.task_key: campaign}

        self.assertIsNone(
            daemon._scheduler_host_slots_available(
                cfg, running, "job:plugin_operation:resource-digest"
            )
        )
        self.assertIsNone(
            daemon._scheduler_instance_slots_available(
                cfg,
                running,
                root="/srv/mautic",
                task_type="job:plugin_operation:resource-digest",
            )
        )

        store = Mock()
        store.has_running_task_key.return_value = False
        store.add_running.return_value = 2
        process = Mock(pid=456)
        with patch.object(daemon, "_task_locked_args", side_effect=lambda _key, args, **_kwargs: args), patch.object(
            daemon, "_spawn_command", return_value=process
        ):
            submitted = daemon._submit_if_slot(
                config=cfg,
                store=store,
                running=running,
                root="/srv/mautic",
                task_type="job:plugin_operation:0123456789abcdef0123456789abcdef",
                entity_id=None,
                args=["php", "bin/console", "vendor:stats:update"],
                timeout_sec=0,
                max_parallel_for_type=1,
                popens={},
                bypass_repeat_guard=True,
            )
        self.assertTrue(submitted)
        self.assertEqual(len(running), 2)

    def test_runtime_state_reports_exact_resource_scoped_skip(self) -> None:
        states: dict[str, dict[str, object]] = {}
        store = Mock()
        store.get_runtime_sync.side_effect = lambda key: states.get(key)
        store.put_runtime_sync.side_effect = lambda key, value: states.__setitem__(key, value)
        digest = daemon._plugin_operation_resource_digest("/srv/mautic", "plugin:viber:stats")

        payload = daemon._plugin_operation_runtime_update(
            store,
            digest,
            {
                "root": "/srv/mautic",
                "operation_key": "viber:stats_update",
                "task_id": "stats",
                "resource_key": "plugin:viber:stats",
                "state": "skipped",
                "skipped_reason": "resource_busy:plugin:viber:stats",
                "heartbeat_at": 100.0,
                "stale": False,
            },
        )

        self.assertEqual(payload["schema"], "mcd-plugin-operation-runtime-v1")
        self.assertEqual(payload["skipped_reason"], "resource_busy:plugin:viber:stats")

    def test_state_push_exposes_runtime_without_internal_output_paths(self) -> None:
        store = Mock()
        store.list_runtime_sync.return_value = [
            (
                "plugin_operation_runtime:abc",
                {
                    "schema": "mcd-plugin-operation-runtime-v1",
                    "root": "/srv/mautic",
                    "operation_key": "vendor:stats",
                    "task_id": "stats",
                    "state": "running",
                    "heartbeat_at": 100.0,
                    "_stdout_path": "/private/runtime.stdout",
                },
            )
        ]
        pusher = MCCStatePusher(SimpleNamespace(), runtime_store=store)

        runtime = pusher._plugin_operations_runtime("/srv/mautic")

        self.assertEqual(len(runtime), 1)
        self.assertEqual(runtime[0]["operation_key"], "vendor:stats")
        self.assertNotIn("_stdout_path", runtime[0])


if __name__ == "__main__":
    unittest.main()
