from __future__ import annotations

import unittest
from typing import Any

from mcd_agent.contact_count_report import collect_contact_count_report
from mcd_agent.db import MauticDB
from mcd_agent.models import DBConfig


class FakeDB:
    _safe_table = staticmethod(MauticDB._safe_table)

    def __init__(self, row: dict[str, Any]) -> None:
        self.cfg = DBConfig(
            host="localhost",
            port=3306,
            name="mautic",
            user="mautic",
            password="secret",
            table_prefix="ss",
        )
        self.row = row
        self.query = ""
        self.limit = 0

    def fetch_rows(self, query: str, limit: int = 5000, context: dict[str, str] | None = None) -> list[dict[str, Any]]:
        self.query = query
        self.limit = limit
        return [self.row]


class ContactCountReportTests(unittest.TestCase):
    def test_counts_contacts_with_email_or_mobile_once(self) -> None:
        db = FakeDB(
            {
                "total_contacts": 15,
                "real_contacts": 12,
                "email_only": 5,
                "mobile_only": 3,
                "email_and_mobile": 4,
                "excluded_without_email_or_mobile": 3,
            }
        )

        payload = collect_contact_count_report(db)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["real_contacts"], 12)
        self.assertEqual(payload["email_only"], 5)
        self.assertEqual(payload["mobile_only"], 3)
        self.assertEqual(payload["email_and_mobile"], 4)
        self.assertEqual(payload["excluded_without_email_or_mobile"], 3)
        self.assertIn("FROM `ssleads`", db.query)
        self.assertIn("TRIM(COALESCE(`email`, ''))", db.query)
        self.assertIn("TRIM(COALESCE(`mobile`, ''))", db.query)
        self.assertEqual(db.limit, 1)

    def test_rejects_inconsistent_database_result(self) -> None:
        db = FakeDB(
            {
                "total_contacts": 10,
                "real_contacts": 8,
                "email_only": 4,
                "mobile_only": 2,
                "email_and_mobile": 1,
                "excluded_without_email_or_mobile": 2,
            }
        )

        with self.assertRaisesRegex(RuntimeError, "consistency check failed"):
            collect_contact_count_report(db)


if __name__ == "__main__":
    unittest.main()
