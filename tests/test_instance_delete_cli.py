from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
