from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent import local_mail


class LocalMailTests(unittest.TestCase):
    def test_clean_host_installs_postfix_and_opendkim(self) -> None:
        installed = {"sendmail-bin": False, "postfix": False, "opendkim": False, "opendkim-tools": False}
        commands = []

        def fake_run(args, **_kwargs):
            commands.append(args)
            if args[:2] == ["dpkg-query", "-W"]:
                return (0, "installed") if installed.get(args[-1]) else (1, "")
            if args == ["debconf-set-selections"] or args[:2] == ["apt-get", "update"]:
                return 0, ""
            if args[:4] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"]:
                self.assertEqual(args[4:], ["-y", "postfix", "opendkim", "opendkim-tools"])
                installed.update({"postfix": True, "opendkim": True, "opendkim-tools": True})
                return 0, ""
            self.fail(f"unexpected command: {args}")

        def fake_which(name):
            if name == "sendmail" and installed["postfix"]:
                return "/usr/sbin/sendmail"
            if name == "opendkim" and installed["opendkim"]:
                return "/usr/sbin/opendkim"
            if name == "opendkim-testkey" and installed["opendkim-tools"]:
                return "/usr/sbin/opendkim-testkey"
            return None

        with patch.object(local_mail, "_run", side_effect=fake_run), patch.object(
            local_mail.shutil, "which", side_effect=fake_which
        ):
            self.assertEqual(local_mail._apt_install(), "postfix")
        self.assertIn(["debconf-set-selections"], commands)

    def test_apt_install_adds_only_missing_native_packages(self) -> None:
        installed = {"sendmail-bin": True, "opendkim": False, "opendkim-tools": False}

        def fake_run(args, **_kwargs):
            if args[:2] == ["dpkg-query", "-W"]:
                package = args[-1]
                return (0, "installed") if installed.get(package) else (1, "")
            if args[:2] == ["apt-get", "update"]:
                return 0, ""
            if args[:4] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install"]:
                self.assertEqual(args[4:], ["-y", "opendkim", "opendkim-tools"])
                installed["opendkim"] = True
                installed["opendkim-tools"] = True
                return 0, ""
            self.fail(f"unexpected command: {args}")

        def fake_which(name):
            if name == "sendmail" or installed.get("opendkim" if name == "opendkim" else "opendkim-tools"):
                return "/usr/sbin/" + name
            return None

        with patch.object(local_mail, "_run", side_effect=fake_run), patch.object(
            local_mail.shutil, "which", side_effect=fake_which
        ):
            local_mail._apt_install()

    def test_apt_install_refuses_to_replace_foreign_mta(self) -> None:
        def fake_run(args, **_kwargs):
            if args[:2] == ["dpkg-query", "-W"]:
                return 1, ""
            self.fail(f"package changes must not run: {args}")

        with patch.object(local_mail, "_run", side_effect=fake_run), patch.object(
            local_mail.shutil, "which", side_effect=lambda name: "/usr/sbin/sendmail" if name == "sendmail" else None
        ):
            with self.assertRaisesRegex(RuntimeError, "will not replace"):
                local_mail._apt_install()

    def test_sendmail_transform_disables_legacy_relay_and_masquerade(self) -> None:
        source = """divert(-1)dnl
define(`SMART_HOST', `mail.sales-snap.com')dnl
define(`confDOMAIN_NAME', `old.example.com')dnl
FEATURE(`allmasquerade')dnl
FEATURE(`masquerade_envelope')dnl
MASQUERADE_AS(`sales-snap.com')dnl
MAILER(`smtp')dnl
"""
        result = local_mail._sendmail_direct_text(source, "mail.app.sales-snap.com")
        active = [line for line in result.splitlines() if not line.lstrip().startswith("dnl")]
        self.assertFalse(any("SMART_HOST" in line for line in active))
        self.assertFalse(any("MASQUERADE" in line for line in active))
        self.assertIn("define(`confDOMAIN_NAME', `mail.app.sales-snap.com')dnl", result)
        self.assertEqual(result.count(local_mail._SENDMAIL_BEGIN), 1)
        self.assertLess(result.index(local_mail._SENDMAIL_BEGIN), result.index("MAILER(`smtp')dnl"))

    def test_mautic_patch_uses_quota_wrapper_and_instance_from_domain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local.php"
            path.write_text(
                "<?php return ['parameters'=>['mailer_dsn'=>'smtp://localhost:25','mailer_from_email'=>'hello@example.com','mailer_return_path'=>null]];\n",
                encoding="utf-8",
            )
            with patch.object(local_mail, "_local_php", return_value=path):
                local_mail._configure_mautic("/var/www/app/public_html", "app.sales-snap.com")
            result = path.read_text(encoding="utf-8")
            self.assertIn("mcd-mail-submit%20--instance-domain%3Dapp.sales-snap.com%20--%20-oi%20-t", result)
            self.assertIn("'mailer_from_email'=>'mailer@app.sales-snap.com'", result)
            self.assertIn("'mailer_return_path'=>'bounce@app.sales-snap.com'", result)
            self.assertNotIn("/.mcd/", result)

    def test_quota_counts_recipients_and_rolls_back_sendmail_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config_root = base / "etc"
            state_root = base / "state"
            domains_path = config_root / "domains.json"
            sendmail = base / "sendmail"
            config_root.mkdir()
            domains_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "domains": {
                            "app.sales-snap.com": {
                                "daily_limit": 3,
                                "monthly_limit": 3,
                                "mail_hostname": "mail.app.sales-snap.com",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            sendmail.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
            sendmail.chmod(0o755)
            patches = (
                patch.object(local_mail, "CONFIG_ROOT", config_root),
                patch.object(local_mail, "STATE_ROOT", state_root),
                patch.object(local_mail, "DOMAINS_PATH", domains_path),
                patch.object(local_mail, "QUOTA_DB_PATH", state_root / "quota.sqlite3"),
                patch.object(local_mail, "SENDMAIL_BIN", sendmail),
            )
            for item in patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
            data = b"From: mailer@app.sales-snap.com\nTo: one@example.com, two@example.com\n\nhello\n"
            self.assertEqual(local_mail.submit_local_mail(domain="app.sales-snap.com", sendmail_args=["--", "-t"], data=data), 0)
            self.assertEqual(local_mail.quota_state("app.sales-snap.com")["daily_used"], 2)
            self.assertEqual(local_mail.submit_local_mail(domain="app.sales-snap.com", sendmail_args=["--", "-t"], data=data), 75)

            sendmail.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n", encoding="utf-8")
            sendmail.chmod(0o755)
            one = b"From: mailer@app.sales-snap.com\nTo: three@example.com\n\nhello\n"
            self.assertEqual(local_mail.submit_local_mail(domain="app.sales-snap.com", sendmail_args=["--", "-t"], data=one), 1)
            self.assertEqual(local_mail.quota_state("app.sales-snap.com")["daily_used"], 2)

    def test_recipient_parser_prefers_envelope_recipients(self) -> None:
        data = b"To: header@example.com\n\nhello\n"
        self.assertEqual(
            local_mail._message_recipients(["-oi", "--", "Envelope@Example.com"], data),
            ["envelope@example.com"],
        )


if __name__ == "__main__":
    unittest.main()
