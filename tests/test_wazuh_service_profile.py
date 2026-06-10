from __future__ import annotations

import sys
from types import SimpleNamespace
import types
import unittest

if "pymysql" not in sys.modules:
    pymysql = types.ModuleType("pymysql")
    pymysql.connect = lambda **_kwargs: None
    pymysql.connections = types.SimpleNamespace(Connection=object)
    cursors = types.ModuleType("pymysql.cursors")
    cursors.DictCursor = object
    pymysql.cursors = cursors
    sys.modules["pymysql"] = pymysql
    sys.modules["pymysql.cursors"] = cursors

import mcd_agent.config as agent_config
import mcd_agent.service_profiles as service_profiles
import mcd_agent.wazuh_profile as wazuh_profile


class WazuhServiceProfileTests(unittest.TestCase):
    def test_legacy_component_defaults_backfill_wazuh(self) -> None:
        components = agent_config._normalize_service_profile_components(
            ["php_fpm", "mysql", "apt", "mautic_db_indexes"]
        )

        self.assertIn("wazuh", components)

    def test_wazuh_apply_once_returns_ok_for_applied_profile(self) -> None:
        old_fetch = service_profiles.fetch_service_profile
        old_apply = service_profiles.apply_wazuh_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp: {
                "status": "ok",
                "profile": {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                },
            }
            service_profiles.apply_wazuh_profile = lambda _profile, _cfg, dry_run=False: {
                "status": "applied",
                "dry_run": bool(dry_run),
            }
            res = service_profiles.service_profiles_apply_once(
                SimpleNamespace(cluster_id=""),
                component="wazuh",
                dry_run=False,
            )
        finally:
            service_profiles.fetch_service_profile = old_fetch
            service_profiles.apply_wazuh_profile = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "applied")

    def test_wazuh_apply_once_returns_error_for_failed_profile(self) -> None:
        old_fetch = service_profiles.fetch_service_profile
        old_apply = service_profiles.apply_wazuh_profile
        try:
            service_profiles.fetch_service_profile = lambda _cfg, _comp: {
                "status": "ok",
                "profile": {
                    "enabled": True,
                    "manager_address": "10.0.0.10",
                },
            }
            service_profiles.apply_wazuh_profile = lambda _profile, _cfg, dry_run=False: {
                "status": "error",
                "reason": "manager_unreachable",
            }
            res = service_profiles.service_profiles_apply_once(
                SimpleNamespace(cluster_id=""),
                component="wazuh",
                dry_run=False,
            )
        finally:
            service_profiles.fetch_service_profile = old_fetch
            service_profiles.apply_wazuh_profile = old_apply

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("reason"), "manager_unreachable")

    def test_wazuh_profile_dry_run_stays_planned_without_root(self) -> None:
        old_collect = wazuh_profile.collect_wazuh_agent_state
        try:
            wazuh_profile.collect_wazuh_agent_state = lambda _profile=None: {"installed": False}
            res = wazuh_profile.apply_wazuh_profile({"enabled": True}, SimpleNamespace(), dry_run=True)
        finally:
            wazuh_profile.collect_wazuh_agent_state = old_collect

        self.assertEqual(res.get("status"), "planned")
        self.assertIn("wazuh_agent_install", res.get("actions", []))


if __name__ == "__main__":
    unittest.main()
