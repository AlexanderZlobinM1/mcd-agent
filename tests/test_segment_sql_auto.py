from __future__ import annotations

import unittest

from mcd_agent.segment_sql_auto import detect_auto_sql_segment_rules


class SegmentSQLAutoTests(unittest.TestCase):
    def test_relative_date_filter_uses_local_date_expression(self) -> None:
        filters = (
            'a:3:{i:0;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:5:"email";s:4:"type";s:5:"email";s:8:"operator";s:6:"!empty";'
            's:10:"properties";a:2:{s:6:"filter";N;s:7:"display";N;}s:6:"filter";N;s:7:"display";N;}'
            'i:1;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:21:"registrovani_korisnik";s:4:"type";s:4:"text";s:8:"operator";s:1:"=";'
            's:10:"properties";a:1:{s:6:"filter";s:1:"1";}s:6:"filter";s:1:"1";s:7:"display";N;}'
            'i:2;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:20:"user_newsletter_date";s:4:"type";s:4:"date";s:8:"operator";s:1:"=";'
            's:10:"properties";a:1:{s:6:"filter";s:6:"-1 day";}'
            's:6:"filter";s:12:"today -1 day";s:7:"display";N;}}'
        )

        rules = detect_auto_sql_segment_rules(
            [{"id": 191, "filters": filters, "problem_count": 2}],
            max_clauses=24,
            problem_threshold=2,
            lead_columns={"email", "registrovani_korisnik", "user_newsletter_date"},
        )

        self.assertIn(191, rules)
        sql = rules[191].select_sql
        self.assertIn(
            "DATE(l.`user_newsletter_date`) = DATE(DATE_SUB('{now_local}', INTERVAL 1 DAY))",
            sql,
        )
        self.assertNotIn("'-1 day'", sql)

    def test_absolute_date_filter_uses_date_comparison(self) -> None:
        filters = (
            'a:1:{i:0;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:20:"user_newsletter_date";s:4:"type";s:4:"date";s:8:"operator";s:1:"=";'
            's:10:"properties";a:1:{s:6:"filter";s:10:"2026-06-03";}'
            's:6:"filter";s:10:"2026-06-03";s:7:"display";N;}}'
        )

        rules = detect_auto_sql_segment_rules(
            [{"id": 191, "filters": filters, "problem_count": 2}],
            max_clauses=24,
            problem_threshold=2,
            lead_columns={"user_newsletter_date"},
        )

        self.assertEqual(
            "SELECT l.id AS lead_id FROM {prefix}leads l WHERE "
            "(DATE(l.`user_newsletter_date`) = DATE('2026-06-03'))",
            rules[191].select_sql,
        )

    def test_page_hit_last_days_uses_local_calendar_day_window(self) -> None:
        filters = (
            'a:2:{i:0;a:8:{s:6:"object";s:4:"lead";s:4:"glue";s:3:"and";'
            's:5:"field";s:5:"email";s:4:"type";s:5:"email";s:8:"operator";s:6:"!empty";'
            's:10:"properties";a:2:{s:6:"filter";N;s:7:"display";N;}s:6:"filter";N;s:7:"display";N;}'
            'i:1;a:7:{s:6:"object";s:9:"behaviors";s:4:"glue";s:3:"and";'
            's:5:"field";s:19:"url_in_last_30_days";s:4:"type";s:9:"page_hits";'
            's:8:"operator";s:8:"contains";s:10:"properties";'
            'a:1:{s:6:"filter";a:2:{i:0;s:6:"gaming";i:1;s:7:"it-shop";}}s:7:"display";N;}}'
        )

        rules = detect_auto_sql_segment_rules(
            [{"id": 189, "filters": filters, "problem_count": 2}],
            max_clauses=24,
            problem_threshold=2,
            lead_columns={"email"},
        )

        sql = rules[189].select_sql
        self.assertIn(
            "ph.date_hit >= DATE(DATE_SUB('{now_local}', INTERVAL 30 DAY))",
            sql,
        )
        self.assertNotIn("ph.date_hit >= DATE_SUB('{now_local}', INTERVAL 30 DAY)", sql)


if __name__ == "__main__":
    unittest.main()
