from __future__ import annotations

from types import SimpleNamespace
import unittest

import mcd_agent.service_profiles as service_profiles


class ServiceProfileClusterGuardTests(unittest.TestCase):
    def test_php_fpm_opcache_override_includes_realpath_cache(self) -> None:
        content = service_profiles._build_opcache_override(
            {
                "opcache_memory_mb": 384,
                "php": {
                    "realpath_cache_size_kb": 65536,
                    "realpath_cache_ttl_sec": 900,
                },
            }
        )

        self.assertIn("realpath_cache_size=65536K", content)
        self.assertIn("realpath_cache_ttl=900", content)

    def test_cluster_db_indexes_skip_without_explicit_maintenance_flag(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")

        res = service_profiles.service_profiles_apply_once(
            cfg,
            component="mautic_db_indexes",
            dry_run=False,
        )

        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "cluster_db_maintenance_requires_explicit_operator_flag")
        self.assertEqual(res.get("component"), "mautic_db_indexes")

    def test_cluster_mysql_skip_without_explicit_maintenance_flag(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")

        res = service_profiles.service_profiles_apply_once(
            cfg,
            component="mysql",
            dry_run=False,
        )

        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "cluster_db_maintenance_requires_explicit_operator_flag")
        self.assertEqual(res.get("component"), "mysql")

    def test_cluster_db_indexes_manual_maintenance_flag_allows_apply(self) -> None:
        cfg = SimpleNamespace(cluster_id="ananasrs-prod")
        old_apply = service_profiles.apply_mautic_db_indexes
        try:
            service_profiles.apply_mautic_db_indexes = lambda _cfg, dry_run=False: {
                "status": "planned" if dry_run else "noop"
            }
            res = service_profiles.service_profiles_apply_once(
                cfg,
                component="mautic_db_indexes",
                dry_run=True,
                allow_cluster_db_maintenance=True,
            )
        finally:
            service_profiles.apply_mautic_db_indexes = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "planned")

    def test_single_host_db_indexes_still_apply_without_maintenance_flag(self) -> None:
        cfg = SimpleNamespace(cluster_id="")
        old_detect = service_profiles._mysql_galera_config_detected
        old_apply = service_profiles.apply_mautic_db_indexes
        try:
            service_profiles._mysql_galera_config_detected = lambda: False
            service_profiles.apply_mautic_db_indexes = lambda _cfg, dry_run=False: {
                "status": "planned" if dry_run else "noop"
            }
            res = service_profiles.service_profiles_apply_once(
                cfg,
                component="mautic_db_indexes",
                dry_run=True,
            )
        finally:
            service_profiles._mysql_galera_config_detected = old_detect
            service_profiles.apply_mautic_db_indexes = old_apply

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("apply", {}).get("status"), "planned")


if __name__ == "__main__":
    unittest.main()
