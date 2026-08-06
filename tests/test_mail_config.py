from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import mail_config
from mcd_agent.amazon_mailer_dep import AMAZON_MAILER_PACKAGE, SENDGRID_MAILER_PACKAGE


class MailConfigTests(unittest.TestCase):
    def test_canonical_mcc_host_name_may_differ_from_local_hostname(self) -> None:
        cfg = SimpleNamespace(mcc_host_name="")
        material = {
            "instance_domain": "app.sales-snap.com",
            "current_host_name": "host-203-0-113-10",
            "transport_type": "own_host",
            "settings": {},
            "credentials": {},
            "last_test_recipient": "operator@example.com",
            "name": "Own host",
        }
        with patch.object(mail_config.os, "geteuid", return_value=0), patch.object(
            mail_config, "fetch_mail_profile", return_value=material
        ), patch.object(
            mail_config, "configure_local_mail", return_value={"status": "ok"}
        ) as configure, patch.object(mail_config, "_profile_status"):
            result = mail_config.apply_mail_profile(
                cfg,
                profile_id="profile-1",
                domain="app.sales-snap.com",
                root="/var/www/app/public_html",
            )

        self.assertEqual(result["status"], "ok")
        configure.assert_called_once()

    def test_smtp_dsn_encodes_credentials_and_encryption(self) -> None:
        dsn, packages = mail_config._external_dsn(
            "smtp",
            {"host": "smtp.example.com", "port": 465, "encryption": "tls"},
            {"username": "user@example.com", "password": "p@ss/word"},
        )
        self.assertEqual(dsn, "smtps://user%40example.com:p%40ss%2Fword@smtp.example.com:465")
        self.assertEqual(packages, set())

    def test_ses_api_requires_the_symfony_bridge(self) -> None:
        dsn, packages = mail_config._external_dsn(
            "amazon_ses_api",
            {"region": "eu-central-1"},
            {"access_key": "access", "secret_key": "secret"},
        )
        self.assertEqual(dsn, "ses+api://access:secret@default?region=eu-central-1")
        self.assertEqual(packages, {AMAZON_MAILER_PACKAGE})

    def test_sendgrid_api_requires_the_symfony_bridge(self) -> None:
        dsn, packages = mail_config._external_dsn(
            "sendgrid_api",
            {},
            {"api_key": "SG.secret"},
        )
        self.assertEqual(dsn, "sendgrid+api://SG.secret@default")
        self.assertEqual(packages, {SENDGRID_MAILER_PACKAGE})


if __name__ == "__main__":
    unittest.main()
