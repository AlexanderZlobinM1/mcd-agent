from __future__ import annotations

import unittest

from mcd_agent.config import (
    _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
    _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
    _campaign_rebuilds_due_sql,
    _campaign_triggers_due_sql,
)


class CampaignDueSqlTests(unittest.TestCase):
    def test_trigger_due_catches_date_events_after_publish_down(self) -> None:
        first_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]

        self.assertIn("el.date_triggered IS NULL", first_branch)
        self.assertIn("el.trigger_date <= '{now_utc}'", first_branch)
        self.assertIn("el.trigger_date >= '{window_start_utc_7d}'", first_branch)
        self.assertNotIn("c.publish_down", first_branch)

    def test_trigger_due_does_not_require_is_scheduled_for_date_events(self) -> None:
        first_branch = _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE.split("UNION", 1)[0]
        date_due_pos = first_branch.index("el.trigger_date <= '{now_utc}'")
        scheduled_pos = first_branch.index("el.is_scheduled = 1")

        self.assertLess(date_due_pos, scheduled_pos)
        self.assertIn("OR (el.is_scheduled = 1 AND el.trigger_date IS NULL)", first_branch)

    def test_rebuild_due_has_publish_down_free_date_action_catchup(self) -> None:
        date_action_branch = _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE.split("UNION", 1)[1]

        self.assertIn("ce.trigger_mode = 'date'", date_action_branch)
        self.assertIn("ce.trigger_date <= '{now_utc}'", date_action_branch)
        self.assertIn("ce.trigger_date >= '{window_start_utc_7d}'", date_action_branch)
        self.assertNotIn("c.publish_down", date_action_branch)

    def test_legacy_explicit_trigger_sql_is_migrated(self) -> None:
        legacy = (
            "SELECT DISTINCT q.id FROM ( SELECT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 AND (c.deleted IS NULL) "
            "AND (c.publish_up IS NULL OR c.publish_up <= '{now_local}') "
            "AND (c.publish_down IS NULL OR c.publish_down >= '{now_local}') "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el "
            "WHERE el.campaign_id = c.id AND el.is_scheduled = 1 "
            "AND el.trigger_date <= '{now_utc}' LIMIT 1 ) UNION SELECT c.id "
            "FROM {prefix}campaigns c INNER JOIN {prefix}campaign_leads cl "
            "ON cl.campaign_id = c.id AND cl.date_added >= '{window_start_local_24h}' "
            "WHERE NOT EXISTS ( SELECT 1 FROM {prefix}campaign_lead_event_log el2 "
            "WHERE el2.campaign_id = cl.campaign_id AND el2.rotation <=> cl.rotation LIMIT 1 ) ) q"
        )

        self.assertEqual(
            _campaign_triggers_due_sql({"campaign_triggers_due": legacy}),
            _DEFAULT_SQL_CAMPAIGN_TRIGGERS_DUE,
        )

    def test_legacy_explicit_rebuild_sql_is_migrated(self) -> None:
        legacy = (
            "SELECT DISTINCT c.id FROM {prefix}campaigns c "
            "WHERE c.is_published = 1 "
            "AND (c.publish_down IS NULL OR c.publish_down >= '{now_local}') "
            "AND EXISTS ( SELECT 1 FROM {prefix}campaign_leadlist_xref cx0 "
            "WHERE cx0.campaign_id = c.id LIMIT 1 ) "
            "OR EXISTS ( SELECT 1 FROM {prefix}campaign_events ce "
            "INNER JOIN {prefix}campaign_leads cld ON cld.campaign_id = c.id "
            "WHERE ce.campaign_id = c.id AND ce.trigger_mode = 'date' "
            "AND ce.trigger_date <= '{now_utc}' AND NOT EXISTS ( "
            "SELECT 1 FROM {prefix}campaign_lead_event_log el3 "
            "WHERE el3.campaign_id = cld.campaign_id "
            "AND el3.rotation <=> cld.rotation LIMIT 1 ) LIMIT 1 )"
        )

        self.assertEqual(
            _campaign_rebuilds_due_sql({"campaign_rebuilds_due": legacy}),
            _DEFAULT_SQL_CAMPAIGN_REBUILDS_DUE,
        )


if __name__ == "__main__":
    unittest.main()
