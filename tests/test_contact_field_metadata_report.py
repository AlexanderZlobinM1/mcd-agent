from __future__ import annotations

import unittest
from typing import Any

from mcd_agent.contact_field_metadata_report import (
    SCHEMA,
    collect_contact_field_metadata_report,
    contact_field_metadata_error,
)
from mcd_agent.db import MauticDB
from mcd_agent.models import DBConfig


class FakeDB:
    _safe_table = staticmethod(MauticDB._safe_table)

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cfg = DBConfig(
            host="localhost",
            port=3306,
            name="mautic",
            user="mautic",
            password="secret",
            table_prefix="ss_",
        )
        self.rows = rows
        self.query = ""
        self.limit = 0

    def fetch_rows(self, query: str, limit: int = 5000, context: dict[str, str] | None = None) -> list[dict[str, Any]]:
        self.query = query
        self.limit = limit
        return self.rows


class ContactFieldMetadataReportTests(unittest.TestCase):
    def test_returns_only_approved_field_and_storage_metadata(self) -> None:
        db = FakeDB(
            [
                {
                    "field_alias": "email",
                    "field_label": "Email",
                    "field_classification": "native",
                    "field_type": "email",
                    "field_group": "core",
                    "field_object": "lead",
                    "storage_type": "varchar",
                    "max_length": 191,
                    "numeric_precision": None,
                    "numeric_scale": None,
                },
                {
                    "field_alias": "annual_revenue",
                    "field_label": "Annual revenue",
                    "field_classification": "custom",
                    "field_type": "number",
                    "field_group": "professional",
                    "field_object": "lead",
                    "storage_type": "decimal",
                    "max_length": None,
                    "numeric_precision": "18",
                    "numeric_scale": "2",
                },
            ]
        )

        payload = collect_contact_field_metadata_report(db)

        self.assertEqual(payload["schema"], SCHEMA)
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["capability"], SCHEMA)
        self.assertEqual(payload["mcd_version"], "1.2.39")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["field_count"], 2)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["fields"][0]["classification"], "native")
        self.assertFalse(payload["fields"][0]["custom"])
        self.assertEqual(payload["fields"][0]["type"], "email")
        self.assertEqual(payload["fields"][0]["max_length"], 191)
        self.assertEqual(payload["fields"][1]["numeric_precision"], 18)
        self.assertEqual(payload["fields"][1]["numeric_scale"], 2)
        self.assertEqual(db.limit, 5000)
        self.assertIn("FROM `ss_lead_fields`", db.query)
        self.assertIn("lf.`label` AS field_label", db.query)
        self.assertIn("lf.`is_fixed`", db.query)
        self.assertIn("lf.`field_group`", db.query)
        self.assertIn("lf.`char_length_limit`", db.query)
        self.assertIn("ORDER BY lf.`field_order`", db.query)
        self.assertNotIn("lf.`name`", db.query)
        self.assertNotIn("lf.`fixed`", db.query)
        self.assertNotIn("lf.`group`", db.query)
        self.assertNotIn("lf.`ordering`", db.query)
        self.assertIn("c.`TABLE_NAME` = 'ss_leads'", db.query)
        self.assertNotIn("default_value", db.query.lower())
        self.assertNotIn("properties", db.query.lower())
        self.assertNotIn("SELECT *", db.query.upper())
        self.assertNotIn("UPDATE ", db.query.upper())
        self.assertNotIn("INSERT ", db.query.upper())
        self.assertNotIn("DELETE ", db.query.upper())

    def test_skips_invalid_empty_alias_without_exposing_unrequested_values(self) -> None:
        payload = collect_contact_field_metadata_report(
            FakeDB([{"field_alias": "", "field_label": "ignored", "default_value": "private"}])
        )

        self.assertEqual(payload["field_count"], 0)
        self.assertEqual(payload["fields"], [])
        self.assertNotIn("private", str(payload))

    def test_error_contract_is_structured_for_per_instance_jobs(self) -> None:
        payload = contact_field_metadata_error("database unavailable")

        self.assertEqual(payload["schema"], SCHEMA)
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["fields"], [])
        self.assertEqual(payload["errors"][0]["code"], "collection_failed")
        self.assertTrue(payload["errors"][0]["retryable"])


if __name__ == "__main__":
    unittest.main()
