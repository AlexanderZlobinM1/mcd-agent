import sqlite3
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path

from mcd_agent.daemon import (
    RunningTask,
    TaskStore,
    _publish_segment_monitor_cycle,
    _scheduler_monitor_plan_key,
)


class SchedulerMonitorCycleTests(unittest.TestCase):
    def _read_payload(self, db_path: Path, root: str) -> dict:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload_json FROM runtime_sync WHERE key=?",
            (_scheduler_monitor_plan_key(root),),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        import json

        return json.loads(str(row["payload_json"]))

    def test_segment_cycle_queues_only_not_launched_items_and_resets_after_full_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            store = TaskStore(str(db_path))
            root = "/var/www/mautic"
            cycle_done = {(root, "segment"): {120}}
            running = {
                "segment-51": RunningTask(
                    row_id=1,
                    root=root,
                    task_key=f"{root}|segment|51",
                    task_type="segment",
                    entity_id=51,
                    command_str="php console",
                    timeout_sec=3600,
                    attempts=1,
                    started_at=time.time() - 5,
                    pid=1234,
                )
            }

            _publish_segment_monitor_cycle(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running=running,
                now_ts=1000.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque([51, 63, 120]),
            )

            payload = self._read_payload(db_path, root)
            cycle = payload["cycles"][0]
            self.assertEqual(cycle["queued"], [63])
            self.assertEqual(cycle["done"], [120])
            self.assertEqual(cycle["running"], [51])

            cycle_done[(root, "segment")] = {51, 63, 120}
            _publish_segment_monitor_cycle(
                store=store,
                root=root,
                cycle_done=cycle_done,
                running={},
                now_ts=1010.0,
                seg_sql_ring=deque(),
                seg_resume_ring=deque(),
                seg_prio_ring=deque(),
                seg_reg_ring=deque([51, 63, 120]),
            )

            payload = self._read_payload(db_path, root)
            cycle = payload["cycles"][0]
            self.assertEqual(cycle["queued"], [51, 63, 120])
            self.assertEqual(cycle["done"], [])
            self.assertEqual(cycle["running"], [])


if __name__ == "__main__":
    unittest.main()
