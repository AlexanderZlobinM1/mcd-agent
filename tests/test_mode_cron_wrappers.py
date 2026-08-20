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

    def test_email_spool_consumer_is_not_managed(self) -> None:
        content = (
            "* * * * * php /var/www/example/bin/console mautic:emails:send "
            "--time-limit=55 --lock-name=process1 --lock_mode=flock\n"
        )

        updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 0)
        self.assertEqual(updated, content)

    def test_email_spool_wrapper_is_not_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "mautic-email-spool.sh"
            script.write_text(
                "#!/bin/bash\nphp /var/www/example/bin/console mautic:emails:send --time-limit=55\n",
                encoding="utf-8",
            )
            content = f"* * * * * {script}\n"

            updated, changed = _comment_managed(content, "ts")

        self.assertEqual(changed, 0)
        self.assertEqual(updated, content)

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

        updated, restored = _restore_mautic_email_send_comments(content)

        self.assertEqual(restored, 1)
        self.assertIn(email_send, updated)
        self.assertIn("# * * * * * php /var/www/example/bin/console mautic:segments:update", updated)

    def test_active_reconcile_restores_email_send_and_keeps_segments_managed(self) -> None:
        email_send = "* * * * * php /var/www/example/bin/console mautic:emails:send"
        segment = "* * * * * php /var/www/example/bin/console mautic:segments:update"
        content = (
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            f"# {email_send}\n"
            "# MCD_MANAGED old: disabled by mcd profile=active\n"
            f"# {segment}\n"
        )

        updated, commented, restored = _reconcile_active_managed_content(content, "new")

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
