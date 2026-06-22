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
            ["nginx", "redis", "php8.3-fpm", "mariadb-server", "mariadb-client", "sendmail", "zabbix-agent2", "nginx"]
        )

        self.assertEqual(services, ["nginx", "redis-server", "php8.3-fpm", "mariadb", "sendmail", "zabbix-agent2"])

    def test_zabbix_agent_state_ok_when_dropin_and_service_match(self) -> None:
        old_dpkg = apt_profile._dpkg_installed_versions
        old_read = apt_profile._read_key_value_file
        old_active = apt_profile._service_active
        old_enabled = apt_profile._service_enabled
        old_listening = apt_profile._tcp_port_listening
        old_exists = apt_profile.Path.exists
        try:
            apt_profile._dpkg_installed_versions = lambda **_kwargs: {"zabbix-agent2": "1:7.0.14-1+ubuntu24.04"}
            apt_profile._read_key_value_file = lambda _path: {
                "Server": "65.109.226.152",
                "ServerActive": "65.109.226.152",
                "Hostname": "MauticFarm-02",
                "ListenPort": "10050",
            }
            apt_profile._service_active = lambda _name, **_kwargs: True
            apt_profile._service_enabled = lambda _name, **_kwargs: True
            apt_profile._tcp_port_listening = lambda _port, **_kwargs: True
            apt_profile.Path.exists = lambda self: True if str(self) == "/etc/zabbix/zabbix_agent2.d/99-mcd-server.conf" else old_exists(self)

            state = apt_profile.collect_zabbix_agent_state(
                {
                    "zabbix_agent_enabled": True,
                    "zabbix_agent_server": "65.109.226.152",
                    "zabbix_agent_server_active": "65.109.226.152",
                    "zabbix_agent_hostname": "MauticFarm-02",
                    "zabbix_agent_port": 10050,
                }
            )
        finally:
            apt_profile._dpkg_installed_versions = old_dpkg
            apt_profile._read_key_value_file = old_read
            apt_profile._service_active = old_active
            apt_profile._service_enabled = old_enabled
            apt_profile._tcp_port_listening = old_listening
            apt_profile.Path.exists = old_exists

        self.assertEqual(state["status"], "ok")
        self.assertTrue(state["dropin"]["matches"])
        self.assertTrue(state["service"]["active"])

    def test_zabbix_agent_firewall_sources_normalize_ip_literals_only(self) -> None:
        sources = apt_profile._normalize_ip_networks(["65.109.226.152", "bad.host", "10.0.0.0/24", "65.109.226.152"])

        self.assertEqual(sources, ["65.109.226.152/32", "10.0.0.0/24"])

    def test_nodejs20_satisfied_requires_node20_and_npm(self) -> None:
        old_which = apt_profile.shutil.which
        old_cmd = apt_profile._cmd_first_line
        try:
            apt_profile.shutil.which = lambda name: {"node": "/usr/bin/node", "npm": "/usr/bin/npm"}.get(name)

            def fake_cmd(cmd, **_kwargs):
                if cmd[0] == "/usr/bin/node":
                    return 0, "v20.20.2"
                if cmd[0] == "/usr/bin/npm":
                    return 0, "10.8.2"
                return 1, ""

            apt_profile._cmd_first_line = fake_cmd
            ok, reason = apt_profile._nodejs20_satisfied()
        finally:
            apt_profile.shutil.which = old_which
            apt_profile._cmd_first_line = old_cmd

        self.assertTrue(ok)
        self.assertIn("nodejs20", reason)

    def test_composer_global_rejects_distro_binary(self) -> None:
        old_which = apt_profile.shutil.which
        old_cmd = apt_profile._cmd_first_line
        try:
            apt_profile.shutil.which = lambda name: "/usr/bin/composer" if name == "composer" else None
            apt_profile._cmd_first_line = lambda _cmd, **_kwargs: (0, "Composer version 2.8.0")
            ok, reason = apt_profile._composer_global_satisfied()
        finally:
            apt_profile.shutil.which = old_which
            apt_profile._cmd_first_line = old_cmd

        self.assertFalse(ok)
        self.assertIn("composer_not_global", reason)

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
