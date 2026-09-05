import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from mcd_agent import mautic_upgrade as upgrade


class ReleaseApprovalTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="test", php_bin="php", mautic_run_as_user="www-data")

    def test_exact_live_approval(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"status": "ok", "version": "7.1.3"}).encode()
        with patch.object(upgrade.urllib.request, "urlopen", return_value=response) as request:
            upgrade._require_release_approval(self.config, "7.1.3")
        self.assertIn("version=7.1.3", request.call_args.args[0].full_url)

    def test_blocked_unreachable_and_invalid_responses_fail_closed(self):
        for error in (HTTPError("https://mcc.example", 409, "blocked", {}, None), URLError("offline"), TimeoutError(), ValueError()):
            with self.subTest(error=type(error).__name__), patch.object(upgrade.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(RuntimeError):
                    upgrade._require_release_approval(self.config, "7.2.0")

    def test_wrong_target_approval_rejected(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"ok","version":"7.2.0"}'
        with patch.object(upgrade.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                upgrade._require_release_approval(self.config, "7.1.3")

    def test_no_managed_fallback_when_mcc_is_unavailable(self):
        with patch.object(upgrade, "_release_targets_from_mcc", return_value={}):
            self.assertEqual(upgrade._release_targets(self.config), {})

    def test_denial_precedes_maintenance_and_package_changes(self):
        install = SimpleNamespace(root="/var/www/test", console_path="/var/www/test/bin/console", runtime="host")
        with (
            patch.object(upgrade, "_pick_install_record", return_value=install),
            patch.object(upgrade, "_read_current_version", return_value="7.1.3"),
            patch.object(upgrade, "_require_release_approval", side_effect=RuntimeError("blocked")),
            patch.object(upgrade, "_enter_upgrade_maintenance") as maintenance,
            patch.object(upgrade, "_apply_zip") as apply_zip,
        ):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                upgrade.run_upgrade_apply(config=self.config, root=install.root, mode="zip", yes=True, do_backup=False, with_system_upgrade=False, target_override="7.2.0", allow_minor=True)
            maintenance.assert_not_called()
            apply_zip.assert_not_called()

    def test_six_to_seven_uses_approved_target_without_override(self):
        install = SimpleNamespace(root="/var/www/test", console_path="/var/www/test/bin/console", runtime="host")
        with (
            patch.object(upgrade, "_pick_install_record", return_value=install),
            patch.object(upgrade, "_read_current_version", return_value="6.0.9"),
            patch.object(upgrade, "_release_targets", return_value={"7": {"version": "7.1.3"}}),
            patch.object(upgrade, "_require_release_approval", side_effect=RuntimeError("stop before mutation")) as approval,
        ):
            with self.assertRaisesRegex(RuntimeError, "stop before mutation"):
                upgrade.run_upgrade_apply(config=self.config, root=install.root, mode="composer", yes=True, do_backup=False, with_system_upgrade=False, allow_major=True)
            approval.assert_called_once_with(self.config, "7.1.3")


if __name__ == "__main__":
    unittest.main()
