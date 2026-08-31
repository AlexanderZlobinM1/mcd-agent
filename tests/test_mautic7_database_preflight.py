from __future__ import annotations

import unittest

from mcd_agent.install_readiness import mautic7_database_compatibility


class Mautic7DatabaseCompatibilityTests(unittest.TestCase):
    def test_accepts_supported_mysql_and_mariadb(self) -> None:
        for database in (
            {"engine": "mysql", "version_tuple": [8, 4, 0], "active": True},
            {"engine": "mysql", "version_tuple": [9, 1, 0], "active": True},
            {"engine": "mariadb", "version_tuple": [10, 11, 0], "active": True},
            {"engine": "mariadb", "version_tuple": [11, 4, 5], "active": True},
        ):
            with self.subTest(database=database):
                self.assertTrue(mautic7_database_compatibility(database)[0])

    def test_rejects_incompatible_unknown_and_inactive_database(self) -> None:
        cases = (
            ({"engine": "mysql", "version_tuple": [8, 0, 40], "active": True}, "8.4.0"),
            ({"engine": "mariadb", "version_tuple": [10, 6, 22], "active": True}, "10.11.0"),
            ({"engine": "", "version_tuple": [0, 0, 0], "active": False}, "engine is unknown"),
            ({"engine": "mysql", "version_tuple": [8, 4, 0], "active": False}, "not confirmed active"),
        )
        for database, expected in cases:
            with self.subTest(database=database):
                ok, reason = mautic7_database_compatibility(database)
                self.assertFalse(ok)
                self.assertIn(expected, reason)


if __name__ == "__main__":
    unittest.main()
