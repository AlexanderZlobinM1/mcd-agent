from __future__ import annotations

from types import SimpleNamespace
import sys
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

import mcd_agent.apt_profile as apt_profile


class AptProfileDbRepoTests(unittest.TestCase):
    def test_hosts_local_fqdn_entry_replaces_unqualified_hostname(self) -> None:
        src = "127.0.0.1 localhost\n65.108.101.82 MauticFarm-02\n"

        desired, fqdn = apt_profile._desired_hosts_with_local_fqdn(
            src,
            ip="65.108.101.82",
            hostname="MauticFarm-02",
            suffix="localdomain",
        )

        self.assertEqual(fqdn, "MauticFarm-02.localdomain")
        self.assertIn("65.108.101.82 MauticFarm-02.localdomain MauticFarm-02\n", desired)
        self.assertNotIn("65.108.101.82 MauticFarm-02\n", desired)

    def test_services_for_present_packages_maps_core_daemons(self) -> None:
        services = apt_profile._services_for_present_packages(
            ["nginx", "redis", "php8.3-fpm", "mariadb-server", "mariadb-client", "sendmail", "nginx"]
        )

        self.assertEqual(services, ["nginx", "redis-server", "php8.3-fpm", "mariadb", "sendmail"])

    def test_percona84_setup_uses_lts_channel_and_https_scheme(self) -> None:
        calls: list[str] = []
        old_run = apt_profile.subprocess.run
        try:
            def fake_run(args, **_kwargs):
                calls.append(args[2])
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            apt_profile.subprocess.run = fake_run
            ok, msg = apt_profile._run_percona_repo_setup("ps84", timeout_sec=30)
        finally:
            apt_profile.subprocess.run = old_run

        self.assertTrue(ok)
        self.assertEqual(msg, "percona_repo_setup:ps-84-lts")
        self.assertEqual(len(calls), 1)
        self.assertIn("percona-release setup -y ps-84-lts --scheme https", calls[0])

    def test_percona_cluster84_alias_maps_to_pxc_lts_channel(self) -> None:
        calls: list[str] = []
        old_run = apt_profile.subprocess.run
        try:
            apt_profile.subprocess.run = lambda args, **_kwargs: (
                calls.append(args[2]) or SimpleNamespace(returncode=0, stdout="", stderr="")
            )
            ok, msg = apt_profile._run_percona_repo_setup("pxc84", timeout_sec=30)
        finally:
            apt_profile.subprocess.run = old_run

        self.assertTrue(ok)
        self.assertEqual(msg, "percona_repo_setup:pxc-84-lts")
        self.assertIn("percona-release setup -y pxc-84-lts --scheme https", calls[0])


if __name__ == "__main__":
    unittest.main()
