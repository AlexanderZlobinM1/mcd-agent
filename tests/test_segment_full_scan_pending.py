from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon
from mcd_agent.ring_utils import reconcile_full_scan_pending, reconcile_ring
from mcd_agent.segment_dependencies import segment_dependency_blocked_ids


class FullScanPendingTests(unittest.TestCase):
    def test_dependency_wait_survives_empty_due_refresh_and_inserts_child_task(self) -> None:
        root = "/srv/example"
        pending: set[int] = set()
        published = {1, 2, 3, 4, 5}
        parents = {4: {2, 5}, 5: {2}}
        finished: set[int] = set()
        running = {}
        ring = deque()
        cfg = SimpleNamespace(command_timeout_sec=3600, segment_full_scan_interval_sec=0)
        with tempfile.TemporaryDirectory() as tmp:
            store = daemon.TaskStore(str(Path(tmp) / "state.db"))

            def submit(**kwargs):
                sid = kwargs["entity_id"]
                task = daemon.RunningTask(
                    row_id=0, root=root, task_key=daemon._task_key(root, "segment", sid),
                    task_type="segment", entity_id=sid, command_str="fixture",
                    timeout_sec=3600, attempts=1, started_at=100.0, pid=1000 + sid,
                    manual_request_id=None,
                )
                task.row_id = store.add_running(task)
                running[task.task_key] = task
                return True

            with patch.object(daemon, "_submit_if_slot", side_effect=submit):
                for tick in range(5):
                    plan = reconcile_full_scan_pending(
                        pending, [4, 5, 2, 1, 3] if tick == 0 else [],
                        full_scan=tick == 0, published_ids=published,
                    )
                    ring = reconcile_ring(ring, plan, new_to_front=True)
                    blocked = segment_dependency_blocked_ids(
                        root=root, candidate_ids=set(plan), parents_by_child=parents,
                        running=running, recently_finished=finished,
                    )
                    daemon._fill_from_ring(
                        ring=ring, ring_limit=1, total_limit=1, root=root, task_type="segment",
                        running=running, ring_entities=set(plan), config=cfg, store=store,
                        popens={}, build_args=lambda sid: ["fixture", str(sid)],
                        blocked_entities=blocked, on_launch=pending.discard,
                    )
                    for key, task in list(running.items()):
                        store.finish(task.row_id, state="done", rc=0, note=None)
                        finished.add(task.entity_id)
                        running.pop(key)
                    if 4 not in finished:
                        self.assertIn(4, pending)

            rows = store.conn.execute("SELECT entity_id, state, rc FROM tasks ORDER BY id").fetchall()
            ids = [row["entity_id"] for row in rows]
            self.assertEqual(set(ids), published)
            self.assertLess(ids.index(2), ids.index(5))
            self.assertLess(ids.index(5), ids.index(4))
            self.assertTrue(all(row["state"] == "done" and row["rc"] == 0 for row in rows))
            self.assertEqual(pending, set())
            store.close()

    def test_unpublished_pending_is_removed_but_metadata_failure_preserves_work(self) -> None:
        pending = {4, 5}
        self.assertEqual(reconcile_full_scan_pending(
            pending, [], full_scan=False, published_ids=None,
        ), [4, 5])
        self.assertEqual(reconcile_full_scan_pending(
            pending, [], full_scan=False, published_ids={5},
        ), [5])
        self.assertEqual(pending, {5})

    def test_ordinary_due_query_does_not_create_sticky_full_scan_work(self) -> None:
        pending: set[int] = set()
        self.assertEqual(reconcile_full_scan_pending(
            pending, [4], full_scan=False, published_ids={4},
        ), [4])
        self.assertEqual(pending, set())

    def test_repeated_full_scan_keeps_unlaunched_work(self) -> None:
        pending = {4}
        self.assertEqual(reconcile_full_scan_pending(
            pending, [5, 4], full_scan=True, published_ids={4, 5},
        ), [5, 4])
        self.assertEqual(pending, {4, 5})
