import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent.signals import _shadow_running_tasks, collect_monitor_signals, collect_signals


class SignalsSchedulerHistoryTests(unittest.TestCase):
    def test_shadow_includes_recent_finished_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE tasks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  root TEXT NOT NULL,
                  task_key TEXT NOT NULL,
                  task_type TEXT NOT NULL,
                  entity_id INTEGER,
                  command_str TEXT NOT NULL,
                  pid INTEGER NOT NULL,
                  timeout_sec INTEGER NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 1,
                  state TEXT NOT NULL,
                  note TEXT,
                  started_at REAL NOT NULL,
                  finished_at REAL,
                  rc INTEGER,
                  manual_request_id INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE runtime_sync (
                  key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  updated_at REAL NOT NULL
                )
                """
            )
            now = time.time()
            conn.execute(
                """
                INSERT INTO tasks(root, task_key, task_type, entity_id, command_str, pid, timeout_sec, state, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("/var/www/mautic", "root|segment|61", "segment", 61, "php console", 1001, 0, "running", now - 5),
            )
            conn.execute(
                """
                INSERT INTO tasks(root, task_key, task_type, entity_id, command_str, pid, timeout_sec, state, started_at, finished_at, rc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "/var/www/mautic",
                    "root|segment|110",
                    "segment",
                    110,
                    "php console",
                    1002,
                    0,
                    "done",
                    now - 20,
                    now - 18,
                    0,
                ),
            )
            conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    "scheduler_monitor_plan:abc",
                    '{"root":"/var/www/mautic","updated_at":1234.0,"cycles":[{"task_type":"segment","queued":[51,63],"done":[110],"running":[61],"total":4,"item_variants":{"sql":[51]}}]}',
                    now,
                ),
            )
            conn.commit()
            conn.close()

            cfg = type("Cfg", (), {"state_db_path": str(db_path)})()
            payload = _shadow_running_tasks(cfg)

        self.assertEqual(payload["tracked_total"], 1)
        self.assertEqual(payload["sample"][0]["entity_id"], 61)
        self.assertEqual(payload["recent"][0]["entity_id"], 110)
        self.assertEqual(payload["recent"][0]["rc"], 0)
        self.assertEqual(payload["planned"][0]["root"], "/var/www/mautic")
        self.assertEqual(payload["planned"][0]["queued"], [51, 63])
        self.assertEqual(payload["planned"][0]["done"], [110])
        self.assertEqual(payload["planned"][0]["item_variants"], {"sql": [51]})

    def test_collect_signals_exposes_planned_scheduler_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE tasks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  root TEXT NOT NULL,
                  task_key TEXT NOT NULL,
                  task_type TEXT NOT NULL,
                  entity_id INTEGER,
                  command_str TEXT NOT NULL,
                  pid INTEGER NOT NULL,
                  timeout_sec INTEGER NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 1,
                  state TEXT NOT NULL,
                  note TEXT,
                  started_at REAL NOT NULL,
                  finished_at REAL,
                  rc INTEGER,
                  manual_request_id INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE runtime_sync (
                  key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  updated_at REAL NOT NULL
                )
                """
            )
            now = time.time()
            conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    "scheduler_monitor_plan:def",
                    '{"root":"/var/www/mautic","updated_at":1234.0,"cycles":[{"task_type":"segment","queued":[14],"done":[20,51],"running":[],"total":3,"item_variants":{"sql":[14,20]}}]}',
                    now,
                ),
            )
            conn.commit()
            conn.close()

            cfg = type(
                "Cfg",
                (),
                {
                    "state_db_path": str(db_path),
                    "php_console_stuck_sec": 1800,
                },
            )()
            with (
                patch("mcd_agent.signals._ps_console_processes", return_value=[]),
                patch("mcd_agent.signals._run_journal", return_value=""),
                patch("mcd_agent.signals._detect_php_fpm_units", return_value=[]),
                patch("mcd_agent.signals._count_nginx_file_signals", return_value=(0, 0)),
                patch("mcd_agent.signals._swap_signal", return_value={"level": 0}),
                patch("mcd_agent.signals._filesystem_signal", return_value={"level": 0, "filesystems": {}}),
            ):
                payload = collect_signals(window_min=5, cfg=cfg)

        scheduler = payload["details"]["scheduler"]
        self.assertEqual(scheduler["planned"][0]["root"], "/var/www/mautic")
        self.assertEqual(scheduler["planned"][0]["queued"], [14])
        self.assertEqual(scheduler["planned"][0]["done"], [20, 51])
        self.assertEqual(scheduler["planned"][0]["item_variants"], {"sql": [14, 20]})

    def test_collect_monitor_signals_uses_lightweight_scheduler_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "state.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE tasks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  root TEXT NOT NULL,
                  task_key TEXT NOT NULL,
                  task_type TEXT NOT NULL,
                  entity_id INTEGER,
                  command_str TEXT NOT NULL,
                  pid INTEGER NOT NULL,
                  timeout_sec INTEGER NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 1,
                  state TEXT NOT NULL,
                  note TEXT,
                  started_at REAL NOT NULL,
                  finished_at REAL,
                  rc INTEGER,
                  manual_request_id INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE runtime_sync (
                  key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  updated_at REAL NOT NULL
                )
                """
            )
            now = time.time()
            conn.execute(
                """
                INSERT INTO runtime_sync(key, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    "scheduler_monitor_plan:ghi",
                    '{"root":"/var/www/mautic","updated_at":1234.0,"cycles":[{"task_type":"segment","queued":[73],"done":[20],"running":[51],"total":3,"item_variants":{"sql":[73]}}]}',
                    now,
                ),
            )
            conn.commit()
            conn.close()

            cfg = type("Cfg", (), {"state_db_path": str(db_path)})()
            with patch("mcd_agent.signals._ps_console_processes", return_value=[]):
                payload = collect_monitor_signals(cfg)

        self.assertTrue(payload["monitor_only"])
        scheduler = payload["details"]["scheduler"]
        self.assertEqual(scheduler["planned"][0]["queued"], [73])
        self.assertEqual(scheduler["planned"][0]["done"], [20])
        self.assertEqual(scheduler["planned"][0]["running"], [51])
        self.assertEqual(scheduler["planned"][0]["item_variants"], {"sql": [73]})
        self.assertEqual(payload["details"]["php_console_recent"], [])


if __name__ == "__main__":
    unittest.main()
