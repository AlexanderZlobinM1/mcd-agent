from __future__ import annotations

import unittest

from mcd_agent.mautic_db_indexes import MANAGED_INDEXES, _add_index_sql, _connect, _index_already_present
from mcd_agent.models import DBConfig


class MauticDbIndexesTests(unittest.TestCase):
    def test_managed_index_sql_uses_prefix_and_online_ddl(self) -> None:
        sql = _add_index_sql("ss_", MANAGED_INDEXES[0])

        self.assertIn("ALTER TABLE `ss_lead_lists_leads`", sql)
        self.assertIn("`idx_mcd_lll_list_date_removed_lead`", sql)
        self.assertIn("`leadlist_id`, `date_added`, `manually_removed`, `lead_id`", sql)
        self.assertIn("ALGORITHM=INPLACE, LOCK=NONE", sql)

    def test_existing_index_detected_by_same_columns_under_other_name(self) -> None:
        present, reason = _index_already_present(
            {"custom_idx": ("leadlist_id", "date_added", "manually_removed", "lead_id")},
            MANAGED_INDEXES[0],
        )

        self.assertTrue(present)
        self.assertEqual(reason, "columns_match:custom_idx")

    def test_localhost_connection_prefers_unix_socket(self) -> None:
        captured = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            class Dummy:
                pass
            return Dummy()

        import mcd_agent.mautic_db_indexes as mod

        old_exists = mod.os.path.exists
        old_connect = mod.pymysql.connect
        try:
            mod.os.path.exists = lambda path: path == "/run/mysqld/mysqld.sock"
            mod.pymysql.connect = fake_connect
            _connect(DBConfig(host="localhost", port=3306, name="db", user="u", password="p", table_prefix=""))
        finally:
            mod.os.path.exists = old_exists
            mod.pymysql.connect = old_connect

        self.assertEqual(captured.get("unix_socket"), "/run/mysqld/mysqld.sock")
        self.assertNotIn("host", captured)


if __name__ == "__main__":
    unittest.main()
