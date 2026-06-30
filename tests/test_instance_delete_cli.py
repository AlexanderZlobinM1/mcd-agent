from __future__ import annotations

import contextlib
import io
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import cli


class _Inventory:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def list_instances(self) -> list[object]:
        return list(self._rows)


class InstanceDeleteCliTests(unittest.TestCase):
    def test_delete_can_use_absolute_root_after_inventory_row_is_gone(self) -> None:
        cfg = SimpleNamespace(state_db_path="/tmp/mcd-state.db")
        root = "/var/www/ss/public_html"
        with (
            patch.object(cli, "InstanceInventory", return_value=_Inventory([])),
            patch.object(cli, "ensure_seeded"),
        ):
            self.assertEqual(
                cli._select_root_for_ops(cfg, root, allow_missing_absolute=True),
                root,
            )

    def test_other_operations_still_require_inventory_match(self) -> None:
        cfg = SimpleNamespace(state_db_path="/tmp/mcd-state.db")
        with (
            patch.object(cli, "InstanceInventory", return_value=_Inventory([])),
            patch.object(cli, "ensure_seeded"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Mautic install not found"):
                cli._select_root_for_ops(cfg, "/var/www/ss/public_html")

    def test_instance_delete_main_allows_missing_absolute_root(self) -> None:
        calls: list[dict[str, object]] = []

        def select_root(_cfg: object, root: str | None, **kwargs: object) -> str:
            calls.append({"root": root, **kwargs})
            return str(root)

        with (
            patch.object(sys, "argv", [
                "mcd-cli",
                "instance-delete",
                "--root",
                "/var/www/ss/public_html",
                "--delete-files",
                "--yes",
                "--json",
            ]),
            patch.object(cli, "load_config", return_value=SimpleNamespace(state_db_path="/tmp/mcd-state.db")),
            patch.object(cli, "maybe_notify_update", return_value=None),
            patch.object(cli, "_select_root_for_ops", side_effect=select_root),
            patch.object(cli, "delete_instance_artifacts", return_value={"status": "ok"}),
            patch.object(cli, "_push_state_after_change"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(calls, [{"root": "/var/www/ss/public_html", "allow_missing_absolute": True}])

    def test_manual_command_main_still_requires_inventory_match(self) -> None:
        calls: list[dict[str, object]] = []

        def select_root(_cfg: object, root: str | None, **kwargs: object) -> str:
            calls.append({"root": root, **kwargs})
            return str(root)

        with (
            patch.object(sys, "argv", [
                "mcd-cli",
                "cache:clear",
                "--root",
                "/var/www/ss/public_html",
            ]),
            patch.object(cli, "load_config", return_value=SimpleNamespace(state_db_path="/tmp/mcd-state.db")),
            patch.object(cli, "maybe_notify_update", return_value=None),
            patch.object(cli, "_select_root_for_ops", side_effect=select_root),
            patch.object(cli, "_run_manual_command_with_scheduler", return_value=(0, "")),
            patch.object(cli, "_push_state_after_change"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(calls, [{"root": "/var/www/ss/public_html"}])


if __name__ == "__main__":
    unittest.main()
