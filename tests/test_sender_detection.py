from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcd_agent.state_push import _detect_sender_profile


class SenderDetectionTests(unittest.TestCase):
    def _write_local_php(self, root: Path, body: str) -> None:
        config = root / "config"
        config.mkdir(parents=True)
        (config / "local.php").write_text("<?php $parameters = [" + body + "];\n", encoding="utf-8")

    def test_own_host_sendmail_is_detected_from_local_php(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_php(
                root,
                "'mailer_dsn'=>'sendmail://default?command=%2Fusr%2Flocal%2Fbin%2Fmcd-mail-submit',"
                "'mailer_from_email'=>'mailer@app.example.com','mailer_return_path'=>'bounce@app.example.com'",
            )

            result = _detect_sender_profile(str(root), [])

        self.assertEqual(result["sender_key"], "own_host")
        self.assertEqual(result["sender_type"], "own host")
        self.assertTrue(result["sender_config"]["configured"])
        self.assertEqual(result["sender_config"]["from_email"], "mailer@app.example.com")

    def test_zender_plugin_is_not_classified_as_email_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_php(root, "'mailer_dsn'=>'','mailer_from_email'=>''")

            result = _detect_sender_profile(str(root), [{"bundle": "MauticZenderBundle", "version": "1.2.10"}])

        self.assertEqual(result["sender_key"], "unknown")
        self.assertEqual(result["sender_type"], "unknown")
        self.assertFalse(result["sender_config"]["configured"])
        self.assertNotIn("Zender", result["sender_title"])

    def test_ses_snapshot_contains_only_safe_configuration_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_local_php(
                root,
                "'mailer_dsn'=>'ses+api://ACCESS:SECRET@default?region=eu-central-1',"
                "'mailer_from_email'=>'mailer@app.example.com','mailer_from_name'=>'Sales Snap',"
                "'mailer_return_path'=>'bounce@app.example.com'",
            )

            result = _detect_sender_profile(str(root), [])

        observed = result["sender_config"]
        self.assertEqual(result["sender_type"], "ses+api")
        self.assertEqual(observed["region"], "eu-central-1")
        self.assertEqual(observed["credentials_present"], {"username": True, "password": True})
        self.assertNotIn("ACCESS", str(observed))
        self.assertNotIn("SECRET", str(observed))


if __name__ == "__main__":
    unittest.main()
