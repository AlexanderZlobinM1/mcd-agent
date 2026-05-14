from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.backup import _cluster_prepared_mysql_datadir_from_cmdline


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(backup_cluster_local_root_dir="/mnt/data/backup/local/ananasrs")


class PreparedOffsiteMysqlDetectionTest(unittest.TestCase):
    def test_detects_mcd_prepared_offsite_mysql(self) -> None:
        cmdline = (
            "/usr/sbin/mysqld --no-defaults "
            "--datadir=/mnt/data/backup/local/ananasrs/db/offsite-mysql/prepared-20260513-093830 "
            "--socket=/tmp/mcd-offsite-mysql-r6uxlec_/mysql.sock "
            "--skip-networking --skip-log-bin --skip-grant-tables --read-only=ON --super-read-only=ON"
        )

        datadir = _cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline)

        self.assertEqual(
            datadir,
            Path("/mnt/data/backup/local/ananasrs/db/offsite-mysql/prepared-20260513-093830"),
        )

    def test_rejects_production_mysql(self) -> None:
        cmdline = "/usr/sbin/mysqld --datadir=/var/lib/mysql --socket=/run/mysqld/mysqld.sock"

        self.assertIsNone(_cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline))

    def test_rejects_non_mcd_socket_even_under_backup_root(self) -> None:
        cmdline = (
            "/usr/sbin/mysqld --no-defaults "
            "--datadir=/mnt/data/backup/local/ananasrs/db/offsite-mysql/prepared-20260513-093830 "
            "--socket=/tmp/mysql.sock --skip-networking --skip-grant-tables"
        )

        self.assertIsNone(_cluster_prepared_mysql_datadir_from_cmdline(_cfg(), cmdline))


if __name__ == "__main__":
    unittest.main()
