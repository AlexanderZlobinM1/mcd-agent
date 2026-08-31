from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from mcd_agent import daemon
from mcd_agent.db import MauticDB


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


if __name__ == "__main__":
    unittest.main()
