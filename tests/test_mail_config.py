from __future__ import annotations

import unittest

from mcd_agent import mail_config
from mcd_agent.amazon_mailer_dep import AMAZON_MAILER_PACKAGE, SENDGRID_MAILER_PACKAGE


class MailConfigTests(unittest.TestCase):
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
