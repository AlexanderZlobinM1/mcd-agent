from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import security_blocklist


class SecurityBlocklistTests(unittest.TestCase):
    def test_managed_sshd_jail_is_strict_and_uses_a_unique_action_name(self) -> None:
        config = security_blocklist._jail_config(
            enabled=True,
            allowlist={"89.216.113.155", "65.21.54.91"},
            action="iptables[type=allports, name={name}]",
        )

        self.assertIn("maxretry = 3", config)
        self.assertIn("bantime = 7d", config)
        self.assertIn("bantime.maxtime = 30d", config)
        self.assertIn("ignoreip = 127.0.0.1/8 ::1 65.21.54.91 89.216.113.155", config)
        self.assertIn("action = iptables[type=allports, name=mcd-sshd]", config)
        self.assertNotIn("name=sshd]", config)

    def test_managed_mautic_login_jail_is_strict_and_has_a_unique_action_name(self) -> None:
        config = security_blocklist._jail_config(
            enabled=True,
            allowlist={"89.216.113.155", "65.21.54.91"},
            action="iptables[type=allports, name={name}]",
        )

        self.assertIn("[mautic-auth-nginx]", config)
        self.assertIn("filter = mautic-auth-nginx", config)
        self.assertIn("logpath = /var/log/nginx/*access.log", config)
        self.assertIn("maxretry = 3", config)
        self.assertIn("action = iptables[type=allports, name=mcd-mautic-auth-nginx]", config)
        self.assertNotIn("nginx-4xx-scan", config)

    def test_allowlist_comparison_ignores_other_ip_family(self) -> None:
        self.assertFalse(
            security_blocklist._allowlisted(
                "37.27.194.110/32",
                {"2a09:bac5:280f:248c::3a4:5d/128"},
            )
        )
        self.assertFalse(
            security_blocklist._allowlisted(
                "2a09:bac5:280f:248c::3a4:5d/128",
                {"37.27.194.110/32"},
            )
        )

    def test_reconcile_adds_and_removes_exact_central_bans(self) -> None:
        commands: list[list[str]] = []

        def fake_run(*args: str, **_kwargs):
            commands.append(list(args))
            if list(args[-3:]) == ["status", "mcc-global"] or (
                len(args) >= 3 and args[-2:] == ("status", "mcc-global")
            ):
                return SimpleNamespace(
                    returncode=0,
                    stdout="Banned IP list: 8.8.4.4 9.9.9.9\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (
                patch.object(security_blocklist, "FILTER_PATH", root / "filter.conf"),
                patch.object(security_blocklist, "JAIL_PATH", root / "jail.local"),
                patch.object(security_blocklist, "LOG_PATH", root / "mcc.log"),
                patch.object(security_blocklist.os, "geteuid", return_value=0),
                patch.object(
                    security_blocklist,
                    "_ensure_fail2ban_installed",
                    return_value=(False, "/usr/bin/fail2ban-client"),
                ),
                patch.object(security_blocklist, "_wait_for_ssh_firewall_action"),
                patch.object(security_blocklist, "_run", side_effect=fake_run),
            ):
                result = security_blocklist.apply_security_blocklist_profile(
                    {
                        "enabled": True,
                        "blocked": ["8.8.8.8", "9.9.9.9"],
                        "allowlist": ["9.9.9.0/24"],
                        "poll_sec": 45,
                    }
                )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["added"], ["8.8.8.8"])
        self.assertEqual(result["removed"], ["8.8.4.4", "9.9.9.9"])
        self.assertIn(
            ["/usr/bin/fail2ban-client", "set", "mcc-global", "banip", "8.8.8.8"],
            commands,
        )
        self.assertIn(
            [
                "/usr/bin/fail2ban-client",
                "set",
                "mcc-global",
                "unbanip",
                "8.8.4.4",
                "9.9.9.9",
            ],
            commands,
        )

    def test_disabled_profile_does_not_install_fail2ban(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(security_blocklist, "JAIL_PATH", Path(td) / "missing.local"),
                patch.object(security_blocklist.os, "geteuid", return_value=0),
                patch.object(security_blocklist.shutil, "which", return_value=None),
                patch.object(security_blocklist, "_ensure_fail2ban_installed") as install,
            ):
                result = security_blocklist.apply_security_blocklist_profile({"enabled": False})

        self.assertEqual(result["status"], "noop")
        install.assert_not_called()

    def test_large_blocklist_is_applied_in_bounded_batches(self) -> None:
        commands: list[list[str]] = []

        def fake_run(*args: str, **_kwargs):
            commands.append(list(args))
            if len(args) >= 3 and args[-2:] == ("status", "mcc-global"):
                return SimpleNamespace(returncode=0, stdout="Banned IP list:\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        blocked = [f"8.0.{index // 256}.{index % 256}" for index in range(501)]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (
                patch.object(security_blocklist, "FILTER_PATH", root / "filter.conf"),
                patch.object(security_blocklist, "JAIL_PATH", root / "jail.local"),
                patch.object(security_blocklist, "LOG_PATH", root / "mcc.log"),
                patch.object(security_blocklist.os, "geteuid", return_value=0),
                patch.object(
                    security_blocklist,
                    "_ensure_fail2ban_installed",
                    return_value=(False, "/usr/bin/fail2ban-client"),
                ),
                patch.object(security_blocklist, "_wait_for_ssh_firewall_action"),
                patch.object(security_blocklist, "_run", side_effect=fake_run),
            ):
                result = security_blocklist.apply_security_blocklist_profile(
                    {"enabled": True, "blocked": blocked, "allowlist": []}
                )

        ban_commands = [command for command in commands if command[2:4] == ["mcc-global", "banip"]]
        self.assertEqual(result["desired"], 501)
        self.assertEqual(len(ban_commands), 3)
        self.assertTrue(all(len(command[4:]) <= 250 for command in ban_commands))

    def test_bulk_commands_never_mix_ipv4_and_ipv6(self) -> None:
        commands: list[list[str]] = []

        def fake_run(*args: str, **_kwargs):
            commands.append(list(args))
            if len(args) >= 3 and args[-2:] == ("status", "mcc-global"):
                return SimpleNamespace(returncode=0, stdout="Banned IP list:\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (
                patch.object(security_blocklist, "FILTER_PATH", root / "filter.conf"),
                patch.object(security_blocklist, "JAIL_PATH", root / "jail.local"),
                patch.object(security_blocklist, "LOG_PATH", root / "mcc.log"),
                patch.object(security_blocklist.os, "geteuid", return_value=0),
                patch.object(
                    security_blocklist,
                    "_ensure_fail2ban_installed",
                    return_value=(False, "/usr/bin/fail2ban-client"),
                ),
                patch.object(security_blocklist, "_wait_for_ssh_firewall_action"),
                patch.object(security_blocklist, "_run", side_effect=fake_run),
            ):
                security_blocklist.apply_security_blocklist_profile(
                    {
                        "enabled": True,
                        "blocked": ["8.8.8.8", "2001:4860:4860::8888"],
                        "allowlist": [],
                    }
                )

        ban_commands = [command for command in commands if command[2:4] == ["mcc-global", "banip"]]
        self.assertEqual(len(ban_commands), 2)
        self.assertEqual([len(command[4:]) for command in ban_commands], [1, 1])

    def test_failed_fetch_never_changes_local_bans(self) -> None:
        with (
            patch.object(
                security_blocklist,
                "fetch_security_blocklist",
                return_value={"status": "error", "reason": "snapshot stale"},
            ),
            patch.object(security_blocklist, "apply_security_blocklist_profile") as apply,
        ):
            result = security_blocklist.sync_security_blocklist_once(SimpleNamespace())

        self.assertEqual(result["status"], "error")
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
