from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent import web_ingress_firewall


class ManagedWebFirewallLoopbackTests(unittest.TestCase):
    def test_skips_hosts_without_the_legacy_mcd_web_firewall(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch.object(web_ingress_firewall, "WEB_FIREWALL_SERVICE", base / "missing.service"), patch.object(
                web_ingress_firewall, "LOOPBACK_HELPER", base / "helper"
            ), patch.object(web_ingress_firewall, "_run") as run:
                result = web_ingress_firewall.ensure_managed_web_firewall_loopback()

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "managed_web_firewall_absent")
            run.assert_not_called()

    def test_installs_a_loopback_guard_after_the_managed_web_firewall(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent_service = base / "mcd-web-firewall.service"
            helper = base / "libexec" / "mcd-web-firewall-loopback"
            loopback_service = base / "mcd-web-firewall-loopback.service"
            dropin = base / "mcd-web-firewall.service.d" / "20-loopback.conf"
            parent_service.write_text("[Service]\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(args: list[str], **_kwargs: object) -> tuple[int, str]:
                calls.append(args)
                return 0, ""

            with patch.object(web_ingress_firewall, "WEB_FIREWALL_SERVICE", parent_service), patch.object(
                web_ingress_firewall, "LOOPBACK_HELPER", helper
            ), patch.object(web_ingress_firewall, "LOOPBACK_SERVICE", loopback_service), patch.object(
                web_ingress_firewall, "WEB_FIREWALL_DROPIN", dropin
            ), patch.object(web_ingress_firewall.os, "geteuid", return_value=0), patch.object(
                web_ingress_firewall, "_run", side_effect=fake_run
            ):
                result = web_ingress_firewall.ensure_managed_web_firewall_loopback()

            self.assertEqual(result["status"], "ok")
            helper_text = helper.read_text(encoding="utf-8")
            self.assertIn("MCD_CF_WEB", helper_text)
            self.assertIn("MCD_CF_WEB6", helper_text)
            self.assertIn("-C INPUT -i lo -j ACCEPT", helper_text)
            self.assertIn("-D INPUT -i lo -j ACCEPT", helper_text)
            self.assertIn("-I INPUT 1 -i lo -j ACCEPT", helper_text)
            self.assertIn("After=mcd-web-firewall.service", loopback_service.read_text(encoding="utf-8"))
            dropin_text = dropin.read_text(encoding="utf-8")
            self.assertIn("Wants=mcd-web-firewall-loopback.service", dropin_text)
            self.assertIn("Before=mcd-web-firewall-loopback.service", dropin_text)
            self.assertIn(["systemctl", "daemon-reload"], calls)
            self.assertIn(["systemctl", "restart", "mcd-web-firewall-loopback"], calls)
