import unittest

from mcd_agent.mode import _comment_empty_leads_cleanup, _managed_empty_leads_cleanup_schedules


class EmptyLeadsCleanupScheduleTests(unittest.TestCase):
    def test_monthly_legacy_cron_migrates_as_cron_expr(self):
        content = '0 2 1 * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;" > /dev/null 2>&1\n'
        updated, changed, interval_sec, cron_exprs = _comment_empty_leads_cleanup(content, 'ts')
        self.assertEqual(changed, 1)
        self.assertEqual(interval_sec, 900)
        self.assertEqual(cron_exprs, ['0 2 1 * *'])
        self.assertIn('# MCD_MANAGED ts', updated)

    def test_interval_legacy_cron_migrates_as_interval(self):
        content = '*/15 * * * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;" > /dev/null 2>&1\n'
        _updated, changed, interval_sec, cron_exprs = _comment_empty_leads_cleanup(content, 'ts')
        self.assertEqual(changed, 1)
        self.assertEqual(interval_sec, 900)
        self.assertEqual(cron_exprs, [])

    def test_existing_managed_comment_can_recover_cron_expr(self):
        content = '# MCD_MANAGED ts: disabled empty leads cleanup by mcd profile=active\n# 0 2 1 * * php /var/www/mautic/bin/console doctrine:query:sql "DELETE FROM ss_leads WHERE email IS NULL AND mobile IS NULL;" > /dev/null 2>&1\n'
        interval_sec, cron_exprs = _managed_empty_leads_cleanup_schedules(content)
        self.assertEqual(interval_sec, 0)
        self.assertEqual(cron_exprs, ['0 2 1 * *'])


if __name__ == '__main__':
    unittest.main()
