from __future__ import annotations

import unittest
import tempfile
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import daemon as daemon_mod
from mcd_agent.daemon import (
    CampaignTriggerProgressSnapshot,
    RunningTask,
    SQLSegmentRule,
    TaskStore,
    _CAMPAIGN_REBUILD_FINISHED_AT,
    _campaign_pressure_active,
    _campaign_trigger_event_log_due_exists_sql,
    _campaign_trigger_event_log_progress_sql,
    _campaign_trigger_progress_watchdog,
    _campaign_trigger_waits_for_rebuild,
    _effective_segment_slot_limit,
    _fill_from_ring,
    _mark_campaign_rebuild_finished,
    _mark_campaign_trigger_finished,
    _merge_campaign_trigger_audit_ids,
    _published_segment_whitelist_ids,
    _run_sql_segment_ring,
    _segment_shared_slots_available,
    _segment_task_limit_after_import,
    _segment_whitelist_effective_setting,
    _sync_segment_whitelist_file,
    _submit_import_if_segment_slot,
    _task_key,
    _task_repeat_interval_sec,
)
from mcd_agent.ring_utils import advance_ring_after_launch


class CampaignRingDispatchTests(unittest.TestCase):
    def test_campaign_launch_can_remove_audit_only_id_from_current_ring(self) -> None:
        ring = deque([3, 4, 5])

        advance_ring_after_launch(ring, 3, remove_on_launch=True)

        self.assertEqual(list(ring), [4, 5])

    def test_fill_from_ring_skips_stale_campaign_without_launching(self) -> None:
        ring = deque([136])
        cfg = SimpleNamespace(campaign_trigger_min_repeat_sec=0, campaign_trigger_audit_interval_sec=0)
        store = Mock()
        running: dict[str, RunningTask] = {}

        launched = _fill_from_ring(
            ring=ring,
            ring_limit=1,
            total_limit=1,
            root="/var/www/site",
            task_type="campaign_trigger",
            running=running,
            ring_entities={136},
            config=cfg,
            store=store,
            popens={},
            build_args=Mock(return_value=["php", "bin/console", "mautic:campaigns:trigger", "-i", "136"]),
            should_skip=lambda eid: True,
            remove_on_launch=True,
        )

        self.assertEqual(launched, 0)
        self.assertEqual(list(ring), [])
        store.add_running.assert_not_called()

    def test_campaign_trigger_progress_watchdog_kills_done_stale_process(self) -> None:
        root = "/var/www/site"
        key = _task_key(root, "campaign_trigger", 136)
        task = RunningTask(
            row_id=42,
            root=root,
            task_key=key,
            task_type="campaign_trigger",
            entity_id=136,
            command_str="php bin/console mautic:campaigns:trigger -i 136",
            timeout_sec=0,
            attempts=1,
            started_at=100.0,
            pid=999999,
        )
        running = {key: task}
        store = Mock()
        snapshot = CampaignTriggerProgressSnapshot(
            due_event_logs=0,
            due_root_actions=0,
            pending_event_logs=0,
            triggered_event_logs=4439,
            max_triggered_at="2026-06-12 08:25:59",
        )
        state = {
            key: {
                "last_check": 0.0,
                "progress_key": snapshot.progress_key(),
                "stable_checks": 1,
            }
        }
        cfg = SimpleNamespace(
            campaign_trigger_progress_watchdog_enabled=True,
            campaign_trigger_progress_watchdog_grace_sec=1,
            campaign_trigger_progress_watchdog_interval_sec=10,
            campaign_trigger_progress_watchdog_stable_checks=2,
            segment_kill_grace_sec=1,
        )

        with (
            patch.object(daemon_mod, "_campaign_trigger_progress_snapshot", return_value=snapshot),
            patch.object(daemon_mod, "_kill_pid") as kill_pid,
        ):
            stopped = _campaign_trigger_progress_watchdog(
                config=cfg,
                store=store,
                running=running,
                popens={},
                task=task,
                key=key,
                db_configs_by_root={root: object()},
                mautic_timezones_by_root={root: "Europe/Belgrade"},
                state_by_key=state,
                now_ts=500.0,
            )

        self.assertTrue(stopped)
        kill_pid.assert_called_once_with(999999, 1)
        store.finish.assert_called_once_with(42, state="done", rc=0, note="killed_no_due_no_progress")
        self.assertNotIn(key, running)
        self.assertNotIn(key, state)

    def test_campaign_trigger_guard_counts_prescheduled_rows_as_due(self) -> None:
        due_sql = _campaign_trigger_event_log_due_exists_sql(21)
        progress_sql = _campaign_trigger_event_log_progress_sql(21)

        self.assertIn("el.date_triggered IS NULL", due_sql)
        self.assertIn("el.date_triggered < el.trigger_date", due_sql)
        self.assertIn("el.is_scheduled = 1", due_sql)
        self.assertIn("el.trigger_date <= '{now_utc}'", due_sql)
        self.assertIn("el.trigger_date <= '{now_local}'", due_sql)

        self.assertIn("el.date_triggered < el.trigger_date", progress_sql)
        self.assertIn("pending_event_logs", progress_sql)

    def test_non_campaign_rings_keep_round_robin_rotation(self) -> None:
        ring = deque([3, 4, 5])

        advance_ring_after_launch(ring, 3)

        self.assertEqual(list(ring), [4, 5, 3])

    def test_remove_after_prior_rotation_still_deletes_launched_id(self) -> None:
        ring = deque([3, 4, 5])
        ring.rotate(-1)

        advance_ring_after_launch(ring, 3, remove_on_launch=True)

        self.assertEqual(list(ring), [4, 5])

    def test_campaign_trigger_repeat_guard_uses_audit_interval_floor(self) -> None:
        cfg = SimpleNamespace(
            campaign_trigger_min_repeat_sec=10,
            campaign_trigger_audit_interval_sec=300,
        )

        self.assertEqual(_task_repeat_interval_sec(cfg, "campaign_trigger"), 300)

    def test_campaign_trigger_audit_ids_persist_between_due_sql_cycles(self) -> None:
        audit_ids = [684, 683, 681, 678, 668, 667, 657, 656]

        first_plan = _merge_campaign_trigger_audit_ids([684, 683], audit_ids)
        next_plan = _merge_campaign_trigger_audit_ids([684, 683], audit_ids)

        self.assertIn(656, first_plan)
        self.assertIn(656, next_plan)
        self.assertEqual(first_plan.index(656), next_plan.index(656))

    def test_campaign_trigger_waits_for_rebuild_after_plan(self) -> None:
        _CAMPAIGN_REBUILD_FINISHED_AT.clear()
        root = "/var/www/site"

        self.assertTrue(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )

        _mark_campaign_rebuild_finished(root, 656, now_ts=101.0)

        self.assertFalse(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )

        _mark_campaign_trigger_finished(root, 656)

        self.assertTrue(
            _campaign_trigger_waits_for_rebuild(
                root=root,
                campaign_id=656,
                planned_after_ts=100.0,
                running={},
            )
        )

    def test_task_store_persists_last_launch_for_restart_guard(self) -> None:
        with tempfile.NamedTemporaryFile() as tmp:
            store = TaskStore(tmp.name)
            key = _task_key("/var/www/site", "campaign_trigger", 104)
            task = RunningTask(
                row_id=0,
                root="/var/www/site",
                task_key=key,
                task_type="campaign_trigger",
                entity_id=104,
                command_str="php bin/console mautic:campaigns:trigger -i 104",
                timeout_sec=3600,
                attempts=1,
                started_at=12345.0,
                pid=999999,
            )
            store.add_running(task)

            self.assertEqual(store.last_task_started_at(key), 12345.0)

    def test_import_consumes_one_shared_segment_slot(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "segment", 23): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "segment", 23),
                task_type="segment",
                entity_id=23,
                command_str="segment 23",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1001,
            ),
            _task_key(root, "segment", 44): RunningTask(
                row_id=2,
                root=root,
                task_key=_task_key(root, "segment", 44),
                task_type="segment",
                entity_id=44,
                command_str="segment 44",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1002,
            ),
            _task_key(root, "import", None): RunningTask(
                row_id=3,
                root=root,
                task_key=_task_key(root, "import", None),
                task_type="import",
                entity_id=None,
                command_str="import",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1003,
            ),
        }

        self.assertEqual(_segment_shared_slots_available(running, root, 4), 1)
        self.assertEqual(_segment_task_limit_after_import(running, root, 4), 3)

    def test_pending_import_waits_when_shared_segment_slots_are_full(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "segment", idx): RunningTask(
                row_id=idx,
                root=root,
                task_key=_task_key(root, "segment", idx),
                task_type="segment",
                entity_id=idx,
                command_str=f"segment {idx}",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1000 + idx,
            )
            for idx in (23, 44, 55, 123)
        }
        cfg = SimpleNamespace(
            enable_import_polling=True,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_import_template="bin/console mautic:import --limit={import_limit}",
            import_limit=100,
            command_timeout_sec=3600,
        )

        with (
            patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console", "mautic:import"]),
            patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit,
        ):
            launched = _submit_import_if_segment_slot(
                config=cfg,
                store=SimpleNamespace(),
                running=running,
                popens={},
                root=root,
                cluster_import_allowed=True,
                import_pending_count=1,
                segment_slot_limit=4,
                now_ts=100.0,
            )

        self.assertFalse(launched)
        submit.assert_not_called()

    def test_pending_import_claims_next_free_shared_segment_slot(self) -> None:
        root = "/var/www/site"
        running = {
            _task_key(root, "segment", idx): RunningTask(
                row_id=idx,
                root=root,
                task_key=_task_key(root, "segment", idx),
                task_type="segment",
                entity_id=idx,
                command_str=f"segment {idx}",
                timeout_sec=3600,
                attempts=1,
                started_at=1.0,
                pid=1000 + idx,
            )
            for idx in (23, 44, 55)
        }
        cfg = SimpleNamespace(
            enable_import_polling=True,
            php_bin="php",
            mautic_run_as_user="www-data",
            cmd_import_template="bin/console mautic:import --limit={import_limit}",
            import_limit=100,
            command_timeout_sec=3600,
        )

        with (
            patch.object(daemon_mod, "render_mautic_command", return_value=["php", "bin/console", "mautic:import"]),
            patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit,
        ):
            launched = _submit_import_if_segment_slot(
                config=cfg,
                store=SimpleNamespace(),
                running=running,
                popens={},
                root=root,
                cluster_import_allowed=True,
                import_pending_count=2,
                segment_slot_limit=4,
                now_ts=100.0,
            )

        self.assertTrue(launched)
        self.assertEqual(submit.call_args.kwargs["task_type"], "import")
        self.assertEqual(submit.call_args.kwargs["entity_id"], None)
        self.assertEqual(submit.call_args.kwargs["max_parallel_for_type"], 1)

    def test_effective_segment_slot_limit_matches_throttled_profiles(self) -> None:
        cfg = SimpleNamespace(
            segment_mode="id_weighted",
            segment_throttle_whitelist_only=False,
            segment_throttle_whitelist_parallel=1,
            segment_priority_parallel_idle=3,
            segment_regular_parallel_idle=1,
            segment_priority_parallel_throttled=2,
            segment_regular_parallel_throttled=0,
        )

        self.assertEqual(_effective_segment_slot_limit(cfg, False), 4)
        self.assertEqual(_effective_segment_slot_limit(cfg, True), 2)

        cfg.segment_throttle_whitelist_only = True
        self.assertEqual(_effective_segment_slot_limit(cfg, True), 1)

    def test_fill_from_ring_launches_only_one_entity_per_scheduler_pass(self) -> None:
        root = "/var/www/site"
        ring = deque([11, 22, 33])
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=3,
                total_limit=3,
                root=root,
                task_type="segment",
                running={},
                ring_entities={11, 22, 33},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
            )

        self.assertEqual(launched, 1)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["entity_id"], 11)
        self.assertEqual(list(ring), [22, 33, 11])

    def test_fill_from_ring_skips_blocked_dependency_chain_and_launches_independent_segment(self) -> None:
        root = "/var/www/site"
        ring = deque([11, 22, 33])
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)

        with patch.object(daemon_mod, "_submit_if_slot", return_value=True) as submit:
            launched = _fill_from_ring(
                ring=ring,
                ring_limit=3,
                total_limit=3,
                root=root,
                task_type="segment",
                running={},
                ring_entities={11, 22, 33},
                config=cfg,
                store=SimpleNamespace(),
                popens={},
                build_args=lambda sid: ["php", "bin/console", "mautic:segments:update", "-i", str(sid)],
                dynamic_blocked=lambda sid: sid == 11,
            )

        self.assertEqual(launched, 1)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["entity_id"], 22)
        self.assertEqual(list(ring), [33, 11, 22])

    def test_sql_segment_ring_respects_dependency_chain_worker_lock(self) -> None:
        root = "/var/www/site"
        db = SimpleNamespace(rebuild_segment_membership=Mock(return_value={}))
        cfg = SimpleNamespace(segment_sql_ring_enabled=True)
        store = SimpleNamespace()
        ring = deque([22])

        launched = _run_sql_segment_ring(
            config=cfg,
            store=store,
            db=db,
            root=root,
            ring=ring,
            rules={22: SQLSegmentRule(segment_id=22, select_sql="SELECT 1", depends_on=())},
            active_set={22},
            done_set=set(),
            running={},
            sql_ctx={},
            now_ts=100.0,
            now_local=datetime.now(timezone.utc),
            dynamic_blocked=lambda sid: sid == 22,
        )

        self.assertEqual(launched, 0)
        db.rebuild_segment_membership.assert_not_called()
        self.assertEqual(list(ring), [22])

    def test_campaign_pressure_ignores_short_single_campaign(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            segment_throttle_during_campaigns=True,
            enable_campaign_rebuild=True,
            campaign_pressure_min_running_sec=120,
            campaign_pressure_min_running_count=2,
        )
        running = {
            _task_key(root, "campaign_trigger", 104): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 104),
                task_type="campaign_trigger",
                entity_id=104,
                command_str="campaign trigger 104",
                timeout_sec=3600,
                attempts=1,
                started_at=100.0,
                pid=1004,
            )
        }

        self.assertFalse(
            _campaign_pressure_active(
                cfg,
                running,
                root,
                trigger_prio_ring=deque(),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque(),
                rebuild_reg_ring=deque(),
                now_ts=150.0,
            )
        )

    def test_campaign_pressure_throttles_segments_when_campaign_runs_long(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            segment_throttle_during_campaigns=True,
            enable_campaign_rebuild=True,
            campaign_pressure_min_running_sec=120,
            campaign_pressure_min_running_count=2,
        )
        running = {
            _task_key(root, "campaign_trigger", 104): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 104),
                task_type="campaign_trigger",
                entity_id=104,
                command_str="campaign trigger 104",
                timeout_sec=3600,
                attempts=1,
                started_at=100.0,
                pid=1004,
            )
        }

        self.assertTrue(
            _campaign_pressure_active(
                cfg,
                running,
                root,
                trigger_prio_ring=deque(),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque(),
                rebuild_reg_ring=deque(),
                now_ts=221.0,
            )
        )

    def test_campaign_pressure_throttles_segments_when_campaign_count_threshold_is_met(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            segment_throttle_during_campaigns=True,
            enable_campaign_rebuild=True,
            campaign_pressure_min_running_sec=120,
            campaign_pressure_min_running_count=2,
        )
        running = {
            _task_key(root, "campaign_trigger", 104): RunningTask(
                row_id=1,
                root=root,
                task_key=_task_key(root, "campaign_trigger", 104),
                task_type="campaign_trigger",
                entity_id=104,
                command_str="campaign trigger 104",
                timeout_sec=3600,
                attempts=1,
                started_at=100.0,
                pid=1004,
            ),
            _task_key(root, "campaign_rebuild", 105): RunningTask(
                row_id=2,
                root=root,
                task_key=_task_key(root, "campaign_rebuild", 105),
                task_type="campaign_rebuild",
                entity_id=105,
                command_str="campaign rebuild 105",
                timeout_sec=3600,
                attempts=1,
                started_at=100.0,
                pid=1005,
            ),
        }

        self.assertTrue(
            _campaign_pressure_active(
                cfg,
                running,
                root,
                trigger_prio_ring=deque(),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque(),
                rebuild_reg_ring=deque(),
                now_ts=101.0,
            )
        )

    def test_campaign_pressure_ignores_launchable_campaign_queue(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(
            segment_throttle_during_campaigns=True,
            enable_campaign_rebuild=True,
            campaign_pressure_min_running_sec=120,
            campaign_pressure_min_running_count=2,
        )

        self.assertFalse(
            _campaign_pressure_active(
                cfg,
                {},
                root,
                trigger_prio_ring=deque([104]),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque(),
                rebuild_reg_ring=deque(),
                trigger_dynamic_blocked=lambda cid: False,
                now_ts=100.0,
            )
        )

    def test_campaign_pressure_can_be_disabled_for_segment_scheduler(self) -> None:
        root = "/var/www/site"
        cfg = SimpleNamespace(segment_throttle_during_campaigns=False, enable_campaign_rebuild=True)

        self.assertFalse(
            _campaign_pressure_active(
                cfg,
                {},
                root,
                trigger_prio_ring=deque([104]),
                trigger_reg_ring=deque(),
                rebuild_prio_ring=deque([105]),
                rebuild_reg_ring=deque(),
            )
        )

    def test_segment_whitelist_uses_instance_specific_setting(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[10],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={
                "site-a.example.com": {"segment_whitelist": [187, "191"]},
                "default": {"segment_whitelist": [5]},
            },
        )
        inst = SimpleNamespace(
            instance_uid="site-a.example.com",
            root="/var/www/site-a",
            name="site-a",
            primary_domain="site-a.example.com",
            domains=["site-a.example.com"],
        )

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {187, 191})

    def test_segment_whitelist_falls_back_to_default_per_instance(self) -> None:
        cfg = SimpleNamespace(
            disable_whitelist=False,
            segment_whitelist=[10],
            segment_whitelist_file=None,
            segment_whitelist_instance_settings={"default": {"segment_whitelist": [5]}},
        )
        inst = SimpleNamespace(
            instance_uid="site-b.example.com",
            root="/var/www/site-b",
            name="site-b",
            primary_domain="site-b.example.com",
            domains=["site-b.example.com"],
        )

        self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {5})

    def test_segment_whitelist_reads_scoped_common_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/segment-whitelist.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("site-a.example.com: 187 191\n")
                f.write("site-b.example.com: 5\n")
                f.write("9\n")
            cfg = SimpleNamespace(
                disable_whitelist=False,
                segment_whitelist=[],
                segment_whitelist_file=path,
                segment_whitelist_instance_settings={},
            )
            inst = SimpleNamespace(
                instance_uid="site-a.example.com",
                root="/var/www/site-a",
                name="site-a",
                primary_domain="site-a.example.com",
                domains=["site-a.example.com"],
            )

            self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {187, 191})

    def test_segment_whitelist_sync_converts_legacy_file_to_instance_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/segment-whitelist.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("187\n191\n")
            cfg = SimpleNamespace(
                disable_whitelist=False,
                segment_whitelist=[],
                segment_whitelist_file=path,
                segment_whitelist_instance_settings={},
            )
            inst = SimpleNamespace(
                instance_uid="ananasmk.sales-snap.com",
                root="/var/www/ananasmk",
                name="ananasmk",
                primary_domain="ananasmk.sales-snap.com",
                domains=["ananasmk.sales-snap.com"],
            )

            self.assertTrue(_sync_segment_whitelist_file(cfg, [inst]))
            with open(path, "r", encoding="utf-8") as f:
                contents = f.read()
            self.assertIn("ananasmk.sales-snap.com: 187 191", contents)
            self.assertNotIn("\n187\n", contents)
            self.assertEqual(_segment_whitelist_effective_setting(cfg, inst), {187, 191})

    def test_published_segment_whitelist_ids_uses_only_integer_ids(self) -> None:
        class FakeDB:
            def __init__(self) -> None:
                self.query = ""
                self.limit = 0
                self.context = {}

            def fetch_ids(self, query_template, limit, context=None):
                self.query = query_template
                self.limit = limit
                self.context = context or {}
                return [187, 191]

        db = FakeDB()

        ids = _published_segment_whitelist_ids(db, {191, 187, -3, 0}, {"root": "/var/www/site"})

        self.assertEqual(ids, [187, 191])
        self.assertEqual(db.limit, 2)
        self.assertIn("ll.is_published = 1", db.query)
        self.assertIn("ll.id IN (187,191)", db.query)
        self.assertEqual(db.context, {"root": "/var/www/site"})


if __name__ == "__main__":
    unittest.main()
