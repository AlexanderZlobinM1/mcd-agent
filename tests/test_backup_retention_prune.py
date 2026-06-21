from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcd_agent.backup import _backup_retention_candidate_dirs, _prune_by_copies


class BackupRetentionPruneTests(unittest.TestCase):
    def test_retention_candidates_include_dates_and_instance_timestamps_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            for name in ("2026-06-19", "20260620-010203", "notes", ".incomplete-20260621-010203"):
                (parent / name).mkdir()
            (parent / "file.txt").write_text("not a backup\n", encoding="utf-8")

            names = [p.name for p in _backup_retention_candidate_dirs(parent)]

        self.assertEqual(names, ["20260620-010203", "2026-06-19"])

    def test_prune_keeps_protected_current_backup_and_removes_old_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mount = Path(td)
            parent = mount / "backup" / "host-a" / "instances" / "site-a"
            parent.mkdir(parents=True)
            names = [
                "20260618-010000",
                "20260619-010000",
                "20260620-010000",
                "20260621-010000",
            ]
            for name in names:
                backup_dir = parent / name
                backup_dir.mkdir()
                (backup_dir / "mcc-backup-manifest.json").write_text("{}\n", encoding="utf-8")
            index_dir = mount / "mcc-backups-index.d"
            index_dir.mkdir()
            for name in names:
                rel = f"backup/host-a/instances/site-a/{name}"
                (index_dir / f"{name}.json").write_text(
                    json.dumps(
                        {
                            "backup_dir": rel,
                            "manifest_path": f"{rel}/mcc-backup-manifest.json",
                        },
                        ensure_ascii=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

            removed = _prune_by_copies(
                parent,
                2,
                protected={parent / "20260621-010000"},
                mount_path=mount,
            )

            self.assertEqual(removed, ["20260619-010000", "20260618-010000"])
            self.assertTrue((parent / "20260621-010000").exists())
            self.assertTrue((parent / "20260620-010000").exists())
            self.assertFalse((parent / "20260619-010000").exists())
            self.assertFalse((parent / "20260618-010000").exists())
            self.assertTrue((index_dir / "20260621-010000.json").exists())
            self.assertTrue((index_dir / "20260620-010000.json").exists())
            self.assertFalse((index_dir / "20260619-010000.json").exists())
            self.assertFalse((index_dir / "20260618-010000.json").exists())


if __name__ == "__main__":
    unittest.main()
