from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.backup import (
    _BACKUP_RUNTIME_EXCLUDED_PATHS,
    _backup_asset_sha256,
    _backup_manifest_from_marker,
    _prune_backup_retention,
    _verify_backup_asset_sha256,
)
from mcd_agent.cli import _build_parser


class BackupNoPruneIntegrityTests(unittest.TestCase):
    def test_no_prune_flag_is_explicitly_bound_to_backup_run(self) -> None:
        args = _build_parser().parse_args(["backup", "--no-prune", "run", "--json"])
        self.assertEqual(args.op, "run")
        self.assertTrue(args.no_prune)

    def test_no_prune_preserves_completed_and_incomplete_generations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "backup"
            for name in ("2026-09-14", "2026-09-24", ".incomplete-old"):
                path = parent / name
                path.mkdir(parents=True)
                (path / "sentinel").write_text(name, encoding="utf-8")
            cfg = SimpleNamespace(backup_retention_copies=1)
            removed = _prune_backup_retention(
                cfg, parent, Path(tmp), method="mydumper", enabled=False,
            )
            self.assertEqual(removed, [])
            self.assertEqual(
                sorted(item.name for item in parent.iterdir()),
                [".incomplete-old", "2026-09-14", "2026-09-24"],
            )

    def test_manifest_records_archive_scope_integrity_and_no_prune_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            backup_dir = mount / "backup" / "host" / "2026-09-25"
            db_root = backup_dir / "databases"
            db_file = db_root / "tenant" / "metadata"
            db_file.parent.mkdir(parents=True)
            db_file.write_text("verified database dump", encoding="utf-8")
            archive = backup_dir / "files.tar.gz"
            archive.write_bytes(b"verified file archive")
            marker = {
                "host_name": "host",
                "method": "mydumper",
                "files_archive_path": str(archive),
                "files_archive_sha256": _backup_asset_sha256(archive),
                "database_sha256": _backup_asset_sha256(db_root),
                "runtime_excluded_paths": list(_BACKUP_RUNTIME_EXCLUDED_PATHS),
                "retention_pruned": False,
                "dumped_instances": [{"database": "tenant"}],
            }
            manifest = _backup_manifest_from_marker(
                mount_path=mount,
                backup_dir=backup_dir,
                marker=marker,
                kind="mcc.host_backup.mydumper",
            )
            self.assertEqual(manifest["files_asset"]["sha256"], marker["files_archive_sha256"])
            self.assertEqual(manifest["database_sha256"], marker["database_sha256"])
            self.assertFalse(manifest["retention_pruned"])
            self.assertEqual(manifest["runtime_excluded_paths"], _BACKUP_RUNTIME_EXCLUDED_PATHS)
            _verify_backup_asset_sha256(archive, marker["files_archive_sha256"], label="files archive")
            archive.write_bytes(b"corrupted")
            with self.assertRaisesRegex(RuntimeError, "files archive SHA-256"):
                _verify_backup_asset_sha256(archive, marker["files_archive_sha256"], label="files archive")


if __name__ == "__main__":
    unittest.main()
