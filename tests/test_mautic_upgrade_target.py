from __future__ import annotations

import unittest

from mcd_agent.mautic_upgrade import _clean_target_version, _upgrade_target_relation


class MauticUpgradeTargetTests(unittest.TestCase):
    def test_patch_upgrade_is_allowed_without_minor_flag(self) -> None:
        self.assertEqual(_upgrade_target_relation("7.1.1", "7.1.2"), "allowed")

    def test_one_step_minor_requires_flag(self) -> None:
        self.assertEqual(_upgrade_target_relation("7.0.2", "7.1.2"), "blocked_minor")
        self.assertEqual(_upgrade_target_relation("7.0.2", "7.1.2", allow_minor=True), "allowed")
        self.assertEqual(_upgrade_target_relation("5.1.4", "5.2.11", allow_minor=True), "allowed")

    def test_major_and_multi_minor_are_blocked(self) -> None:
        self.assertEqual(_upgrade_target_relation("7.0.2", "8.0.0", allow_minor=True), "blocked_major")
        self.assertEqual(_upgrade_target_relation("7.0.2", "7.2.0", allow_minor=True), "blocked_minor")

    def test_target_must_be_clean_semver(self) -> None:
        self.assertEqual(_clean_target_version("7.1.2"), "7.1.2")
        with self.assertRaises(RuntimeError):
            _clean_target_version("bad 7.1.2")


if __name__ == "__main__":
    unittest.main()
