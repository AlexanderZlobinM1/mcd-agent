from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.contact_stats import collect_contact_stats
from mcd_agent.models import DBConfig, MauticInstall


class FakeMauticDB:
    instances: list["FakeMauticDB"] = []
    fail_counts = False

    def __init__(self, cfg: DBConfig) -> None:
        self.cfg = cfg
        self.fetch_count_calls: list[str] = []
        FakeMauticDB.instances.append(self)

    @staticmethod
    def _safe_table(raw: str) -> str:
        return raw

    def fetch_rows(self, query: str, limit: int = 5000, context: dict[str, str] | None = None):
        table = str((context or {}).get("table_name") or "")
        if table in {"ss_leads", "ss_lead_donotcontact"}:
            return [{"present": 1}]
        return []

    def fetch_count(self, query: str, context: dict[str, str] | None = None) -> int:
        if self.fail_counts:
            raise RuntimeError("db temporarily unavailable")
        self.fetch_count_calls.append(query)
        if "lead_donotcontact" in query:
            return 12
        return 1234


class ContactStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeMauticDB.instances = []
        FakeMauticDB.fail_counts = False

    def _install(self) -> MauticInstall:
        return MauticInstall(
            instance_uid="shop.example",
            name="shop.example",
            root="/var/www/shop/public_html",
            console_path="/var/www/shop/public_html/bin/console",
            db=DBConfig(
                host="localhost",
                port=3306,
                name="mautic",
                user="mautic",
                password="secret",
                table_prefix="ss_",
            ),
        )

    def test_refreshes_counts_then_reuses_hourly_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.contact_stats.MauticDB", FakeMauticDB):
            first = collect_contact_stats([self._install()], state_dir=td, refresh_interval_sec=3600)
            second = collect_contact_stats([self._install()], state_dir=td, refresh_interval_sec=3600)
            self.assertTrue((Path(td) / "contact-stats-cache.json").exists())

        self.assertEqual(first[0]["total_contacts"], 1234)
        self.assertEqual(first[0]["dnc_contacts"], 12)
        self.assertEqual(first[0]["cache_status"], "refreshed")
        self.assertEqual(second[0]["cache_status"], "cached")
        self.assertEqual(len(FakeMauticDB.instances), 1)
        self.assertEqual(len(FakeMauticDB.instances[0].fetch_count_calls), 2)

    def test_refresh_failure_keeps_stale_cached_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.contact_stats.MauticDB", FakeMauticDB):
            first = collect_contact_stats([self._install()], state_dir=td, refresh_interval_sec=60)
            FakeMauticDB.fail_counts = True
            stale = collect_contact_stats([self._install()], state_dir=td, refresh_interval_sec=0)

        self.assertEqual(first[0]["status"], "ok")
        self.assertEqual(stale[0]["total_contacts"], 1234)
        self.assertEqual(stale[0]["dnc_contacts"], 12)
        self.assertEqual(stale[0]["cache_status"], "stale")
        self.assertIn("db temporarily unavailable", stale[0]["last_error"])


if __name__ == "__main__":
    unittest.main()
