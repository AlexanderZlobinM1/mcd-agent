from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcd_agent.config import load_config


class CampaignRuntimeConfigTests(unittest.TestCase):
    def test_default_campaign_limit_is_unlimited(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text("[runtime]\nprofile_name = \"midi\"\n", encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_limit, 0)
        self.assertIn("{campaign_limit_arg}", cfg.cmd_campaign_trigger_template)
        self.assertEqual(cfg.campaign_trigger_audit_interval_sec, 300)

    def test_page_hits_sql_segments_default_to_quiet_window_only(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text("[runtime]\nprofile_name = \"midi\"\n", encoding="utf-8")

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.segment_sql_page_hits_quiet_only)
        self.assertEqual(cfg.segment_sql_page_hits_quiet_hour, 2)
        self.assertEqual(cfg.segment_sql_page_hits_quiet_window_min, 180)
        self.assertEqual(cfg.segment_sql_auto_long_native_min_duration_sec, 600)

    def test_page_hits_sql_quiet_window_can_be_explicitly_disabled(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[runtime]",
                    'profile_name = "midi"',
                    "segment_sql_page_hits_quiet_only = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertFalse(cfg.segment_sql_page_hits_quiet_only)

    def test_legacy_campaign_trigger_template_is_migrated(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[runtime]",
                    'profile_name = "midi"',
                    'campaign_limit = "unlimited"',
                    "[commands]",
                    'campaign_trigger_template = "mautic:campaigns:trigger -i {id} --campaign-limit={campaign_limit} --batch-limit={batch_limit}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_limit, 0)
        self.assertEqual(
            cfg.cmd_campaign_trigger_template,
            "mautic:campaigns:trigger -i {id}{campaign_limit_arg} --batch-limit={batch_limit}",
        )

    def test_legacy_60000_campaign_limit_is_migrated_to_unlimited(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[runtime]",
                    'profile_name = "midi"',
                    "campaign_limit = 60000",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertEqual(cfg.campaign_limit, 0)

    def test_active_campaign_trigger_reenables_legacy_disabled_rebuild(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "tiny"',
                    "[runtime]",
                    "enable_campaign_rebuild = false",
                    "campaign_trigger_regular_parallel = 1",
                    "campaign_rebuild_regular_parallel = 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertTrue(cfg.enable_campaign_rebuild)
        self.assertEqual(cfg.campaign_rebuild_regular_parallel, 1)

    def test_passive_profile_keeps_campaign_rebuild_disabled(self) -> None:
        path = Path(tempfile.mkdtemp()) / "mcd.toml"
        path.write_text(
            "\n".join(
                [
                    "[profile]",
                    'name = "passive"',
                    "[runtime]",
                    "enable_campaign_rebuild = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        cfg = load_config(str(path), allow_recover_from_mcc=False)

        self.assertFalse(cfg.enable_campaign_rebuild)


if __name__ == "__main__":
    unittest.main()
