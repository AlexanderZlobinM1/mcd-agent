from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.fail2ban_guard import (
    BROWSER_ICON_IGNORE_REGEX,
    MCD_BROWSER_ICON_GUARD_MARKER,
    _patched_nginx_4xx_filter,
    ensure_nginx_4xx_browser_icon_guard,
)


class Fail2banBrowserIconGuardTests(unittest.TestCase):
    def test_empty_ignoreregex_receives_browser_icon_guard(self) -> None:
        source = "[Definition]\nfailregex = bad\nignoreregex =\n"

        patched, changed = _patched_nginx_4xx_filter(source)

        self.assertTrue(changed)
        self.assertIn(MCD_BROWSER_ICON_GUARD_MARKER, patched)
        self.assertIn(f"ignoreregex = {BROWSER_ICON_IGNORE_REGEX}", patched)

    def test_existing_ignoreregex_is_preserved_as_multiline_value(self) -> None:
        source = "[Definition]\nfailregex = bad\nignoreregex = ^old$\n"

        patched, changed = _patched_nginx_4xx_filter(source)

        self.assertTrue(changed)
        self.assertIn("ignoreregex = ^old$", patched)
        self.assertIn(f"    {BROWSER_ICON_IGNORE_REGEX}", patched)

    def test_patch_is_idempotent(self) -> None:
        source = "[Definition]\nfailregex = bad\nignoreregex =\n"
        first, first_changed = _patched_nginx_4xx_filter(source)
        second, second_changed = _patched_nginx_4xx_filter(first)

        self.assertTrue(first_changed)
        self.assertFalse(second_changed)
        self.assertEqual(second, first)

    def test_guard_matches_safari_icon_404_but_not_mautic_route_404(self) -> None:
        pattern = re.compile(BROWSER_ICON_IGNORE_REGEX.replace("<HOST>", r"178\.220\.160\.14"))
        safari = (
            '178.220.160.14 - - [10/Aug/2026:12:12:15 +0200] '
            '"GET /apple-touch-icon-precomposed.png HTTP/2.0" 404 12076 "-" "Safari"'
        )
        favicon = (
            '178.220.160.14 - - [10/Aug/2026:12:12:15 +0200] '
            '"HEAD /favicon.ico?refresh=1 HTTP/2.0" 404 0 "-" "Safari"'
        )
        mautic_route = (
            '178.220.160.14 - - [10/Aug/2026:12:12:15 +0200] '
            '"GET /s/unknown HTTP/2.0" 404 12076 "-" "Safari"'
        )

        self.assertRegex(safari, pattern)
        self.assertRegex(favicon, pattern)
        self.assertNotRegex(mautic_route, pattern)

    def test_ensure_writes_filter_and_reloads_jail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "nginx-4xx-scan.conf"
            target.write_text("[Definition]\nfailregex = bad\nignoreregex =\n", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="OK", stderr="")
            with (
                patch("mcd_agent.fail2ban_guard.os.geteuid", return_value=0),
                patch("mcd_agent.fail2ban_guard.shutil.which", return_value="/usr/bin/fail2ban-client"),
                patch("mcd_agent.fail2ban_guard.subprocess.run", return_value=completed) as run,
            ):
                result = ensure_nginx_4xx_browser_icon_guard(filter_path=target)

        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["changed"])
        run.assert_called_once_with(
            ["/usr/bin/fail2ban-client", "reload", "nginx-4xx-scan"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_reload_failure_restores_original_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "nginx-4xx-scan.conf"
            original = "[Definition]\nfailregex = bad\nignoreregex =\n"
            target.write_text(original, encoding="utf-8")
            failed = SimpleNamespace(returncode=1, stdout="", stderr="bad filter")
            restored = SimpleNamespace(returncode=0, stdout="OK", stderr="")
            with (
                patch("mcd_agent.fail2ban_guard.os.geteuid", return_value=0),
                patch("mcd_agent.fail2ban_guard.shutil.which", return_value="/usr/bin/fail2ban-client"),
                patch("mcd_agent.fail2ban_guard.subprocess.run", side_effect=[failed, restored]),
            ):
                result = ensure_nginx_4xx_browser_icon_guard(filter_path=target)
                current = target.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "error")
        self.assertFalse(result["changed"])
        self.assertEqual(current, original)


if __name__ == "__main__":
    unittest.main()
