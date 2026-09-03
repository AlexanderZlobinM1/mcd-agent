from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from mcd_agent.daemon import _cluster_offsite_not_before_reached


class ClusterBackupScheduleTests(unittest.TestCase):
    def test_offsite_waits_for_configured_start_time(self) -> None:
        cfg = SimpleNamespace(
            backup_cluster_offsite_not_before_hour=2,
            backup_cluster_offsite_not_before_minute=30,
        )

        self.assertFalse(_cluster_offsite_not_before_reached(cfg, datetime(2026, 9, 4, 2, 29, 59)))
        self.assertTrue(_cluster_offsite_not_before_reached(cfg, datetime(2026, 9, 4, 2, 30, 0)))
        self.assertTrue(_cluster_offsite_not_before_reached(cfg, datetime(2026, 9, 4, 23, 59, 0)))

    def test_offsite_defaults_to_two_oclock(self) -> None:
        cfg = SimpleNamespace()

        self.assertFalse(_cluster_offsite_not_before_reached(cfg, datetime(2026, 9, 4, 1, 59, 59)))
        self.assertTrue(_cluster_offsite_not_before_reached(cfg, datetime(2026, 9, 4, 2, 0, 0)))


if __name__ == "__main__":
    unittest.main()
