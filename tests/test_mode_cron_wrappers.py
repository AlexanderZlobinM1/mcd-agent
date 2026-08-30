import tempfile
import unittest
from pathlib import Path

from mcd_agent.mode import (
    _comment_mautic_message_queue_cron,
    _comment_managed,
    _managed_mautic_message_queue_migrations,
    _reconcile_active_managed_content,
    _restore_mautic_message_queue_cron,
    _restore_mautic_email_send_comments,
)


class ModeCronWrapperTests(unittest.TestCase):
    def test_managed_mautic_wrapper_script_is_commented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "mautic-segment-update.sh"
            script.write_text(
                "#!/bin/bash\n"
                'CMD="php /var/www/example/public_html/bin/console mautic:segments:update --batch-limit=1000"\n'
                '$CMD >> /var/www/example/public_html/var/logs/mautic-segment.log 2>&1\n',
                encoding="utf-8",
            )
            content = f"0,20,40 4-23 * * * {script}\n"

            updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 1)
        self.assertIn("# MCD_MANAGED ts: disabled by mcd profile=active", updated)
        self.assertIn(f"# 0,20,40 4-23 * * * {script}", updated)

    def test_unrelated_wrapper_script_is_left_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "daily-report.sh"
            script.write_text("#!/bin/bash\n/bin/true\n", encoding="utf-8")
            content = f"0 5 * * * {script}\n"

            updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 0)
        self.assertEqual(updated, content)

    def test_plugin_cron_is_left_for_catalog_reconciler(self) -> None:
        content = (
            "*/10 * * * * cd /var/www/example && php bin/console "
            "mautic:mailru-postmaster:sync --days=2 --no-interaction\n"
        )

        updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 0)
        self.assertEqual(updated, content)

    def test_catalog_plugin_cron_is_left_for_generic_reconciler(self) -> None:
        content = "0 */6 * * * cd /var/www/oracle && php bin/console ohip:sync --no-interaction\n"

        updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 0)
        self.assertEqual(updated, content)

    def test_email_spool_consumer_is_version_gated(self) -> None:
        console = "/var/www/example/bin/console"
        content = f"* * * * * php {console} mautic:emails:send --time-limit=55 --lock-name=process1\n"

        for major in (5, 6, 7):
            with self.subTest(major=major):
                updated, commented, restored = _reconcile_active_managed_content(content, "ts", {console: major})
                self.assertEqual((commented, restored), (1, 0))
                self.assertIn("# " + content.rstrip("\n"), updated)

        for label, versions in (("mautic4", {console: 4}), ("unknown", {}), ("wrong-path", {"/other/bin/console": 7})):
            with self.subTest(label=label):
                updated, commented, restored = _reconcile_active_managed_content(content, "ts", versions)
                self.assertEqual((commented, restored), (0, 0))
                self.assertEqual(updated, content)

    def test_email_spool_wrapper_is_version_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "mautic-email-spool.sh"
            console = "/var/www/example/bin/console"
            script.write_text(
                f"#!/bin/bash\nphp {console} mautic:emails:send --time-limit=55\n",
                encoding="utf-8",
            )
            content = f"* * * * * {script}\n"

            updated, commented, restored = _reconcile_active_managed_content(content, "ts", {console: 6})

        self.assertEqual((commented, restored), (1, 0))
        self.assertIn("# " + content.rstrip("\n"), updated)

    def test_previously_managed_email_spool_consumer_is_restored(self) -> None:
        email_send = (
            "* * * * * php /var/www/example/bin/console mautic:emails:send "
            "--time-limit=55 --lock-name=process1 --lock_mode=flock"
        )
        content = (
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            f"# {email_send}\n"
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            "# * * * * * php /var/www/example/bin/console mautic:segments:update\n"
        )

        updated, restored = _restore_mautic_email_send_comments(
            content,
            {"/var/www/example/bin/console": 4},
        )

        self.assertEqual(restored, 1)
        self.assertIn(email_send, updated)
        self.assertIn("# * * * * * php /var/www/example/bin/console mautic:segments:update", updated)

    def test_previously_managed_email_spool_consumer_stays_disabled_for_newer_or_unknown_version(self) -> None:
        console = "/var/www/example/bin/console"
        content = (
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            f"# * * * * * php {console} mautic:emails:send --time-limit=55\n"
        )

        for versions in ({console: 5}, {console: 6}, {console: 7}, {}):
            with self.subTest(versions=versions):
                updated, restored = _restore_mautic_email_send_comments(content, versions)
                self.assertEqual(restored, 0)
                self.assertEqual(updated, content)

    def test_relative_console_email_send_is_left_unchanged(self) -> None:
        content = "* * * * * cd /var/www/example && php bin/console mautic:emails:send\n"

        updated, commented, restored = _reconcile_active_managed_content(
            content,
            "ts",
            {"/var/www/example/bin/console": 7},
        )

        self.assertEqual((commented, restored), (0, 0))
        self.assertEqual(updated, content)

    def test_active_reconcile_restores_email_send_and_keeps_segments_managed(self) -> None:
        email_send = "* * * * * php /var/www/example/bin/console mautic:emails:send"
        segment = "* * * * * php /var/www/example/bin/console mautic:segments:update"
        content = (
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            f"# {email_send}\n"
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            f"# {segment}\n"
        )

        updated, commented, restored = _reconcile_active_managed_content(
            content,
            "new",
            {"/var/www/example/bin/console": 4},
        )

        self.assertEqual(restored, 1)
        self.assertEqual(commented, 0)
        self.assertIn(email_send, updated)
        self.assertNotIn(f"\n{segment}\n", updated)

    def test_message_queue_cron_is_commented_only_for_managed_modern_root(self) -> None:
        modern_root = "/var/www/modern/public_html"
        legacy_root = "/var/www/legacy/public_html"
        modern = f"*/5 * * * * php {modern_root}/bin/console mautic:messages:send"
        legacy = f"* * * * * php {legacy_root}/bin/console mautic:messages:send"

        updated, changed, migrations = _comment_mautic_message_queue_cron(
            modern + "\n" + legacy + "\n",
            "ts",
            (modern_root,),
        )

        self.assertEqual(changed, 1)
        self.assertEqual(migrations, {modern_root: 300})
        self.assertIn("# " + modern, updated)
        self.assertIn("\n" + legacy + "\n", updated)

    def test_message_queue_wrapper_cron_is_migrated_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp) / "public_html")
            script = Path(tmp) / "messages-send.sh"
            script.write_text(
                f"#!/bin/bash\nphp {root}/bin/console mautic:messages:send\n",
                encoding="utf-8",
            )
            original = f"15 * * * * {script}\n"

            updated, changed, migrations = _comment_mautic_message_queue_cron(
                original,
                "ts",
                (root,),
            )
            recovered = _managed_mautic_message_queue_migrations(updated)
            restored, restored_count = _restore_mautic_message_queue_cron(updated)

        self.assertEqual(changed, 1)
        self.assertEqual(migrations, {root: 3600})
        self.assertEqual(recovered, migrations)
        self.assertEqual(restored_count, 2)
        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
