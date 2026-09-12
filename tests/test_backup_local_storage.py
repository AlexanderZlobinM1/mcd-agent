from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import backup
from mcd_agent.models import DBConfig


def _cfg(tmp: Path, target: Path) -> SimpleNamespace:
    return SimpleNamespace(
        backup_enabled=True,
        backup_method="mydumper",
        backup_storage_kind="local",
        backup_local_path=str(target),
        backup_local_require_mount=True,
        backup_mount_base_dir=str(tmp / "mounts"),
        backup_remote_root_dir="backup",
        backup_host_name="host-test",
        backup_instance_name=None,
        backup_state_dir=str(tmp / "state"),
        backup_lock_dir=str(tmp / "locks"),
        backup_retention_copies=3,
        backup_unmount_timeout_sec=5,
        backup_dump_timeout_sec=60,
        backup_archive_enabled=True,
        backup_archive_name="files.tar.gz",
        backup_ssh_host="",
        backup_ssh_user="",
        backup_ssh_key_file=None,
        backup_ssh_password=None,
        backup_mysql_host=None,
        backup_mysql_port=None,
        backup_mysql_user=None,
        backup_mysql_password=None,
        backup_mysql_database=None,
        backup_mydumper_threads=4,
        backup_mydumper_compress=True,
        backup_myloader_threads=4,
        backup_restore_apply_files=False,
        backup_restore_apply_databases=True,
    )


def _replace_cfg(value: SimpleNamespace, **changes: object) -> SimpleNamespace:
    fields = vars(value).copy()
    fields.update(changes)
    return SimpleNamespace(**fields)


class BackupLocalStorageTests(unittest.TestCase):
    def test_existing_profile_defaults_to_sftp(self) -> None:
        self.assertEqual(backup._backup_storage_kind(SimpleNamespace()), "sftp")

    def test_relative_namespace_rejects_absolute_and_traversal(self) -> None:
        for value in ("/escape", "../escape", "safe/../../escape", "."):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                backup._validate_storage_relative(value, field="remote_root_dir")
        self.assertEqual(
            backup._validate_storage_relative("tenant/backups", field="remote_root_dir"),
            "tenant/backups",
        )

    def test_local_target_requires_active_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw).resolve()
            cfg = _cfg(tmp, tmp / "target")
            Path(cfg.backup_local_path).mkdir()
            fake_stat = SimpleNamespace(st_uid=0, st_mode=0o40750)
            with patch.object(Path, "stat", return_value=fake_stat), patch.object(
                backup, "_mounted", return_value=False
            ):
                with self.assertRaisesRegex(RuntimeError, "active mountpoint"):
                    backup._validate_local_storage_root(cfg)

    def test_profile_accepts_configurable_dump_and_restore_threads(self) -> None:
        self.assertEqual(
            backup._profile_mydumper_payload({"mydumper": {"threads": 4, "myloader_threads": 7}}),
            {"threads": 4, "myloader_threads": 7},
        )

    def test_local_instance_backup_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw).resolve()
            target = tmp / "target"
            source = tmp / "instance"
            target.mkdir()
            source.mkdir()
            (source / "config.php").write_text("persistent\n", encoding="utf-8")
            cfg = _cfg(tmp, target)
            db = DBConfig(host="127.0.0.1", port=3306, name="tenant", user="u", password="p", table_prefix="")
            inst = SimpleNamespace(
                root=str(source), name="tenant", db=db, instance_uid="uid-1",
                primary_domain="tenant.example", mautic_major=5,
            )

            def archive_files(_cfg: object, _inst: object, out: Path) -> Path:
                path = out / "files.tar.gz"
                path.write_bytes(b"files")
                return path

            def dump(_cfg: object, _db: object, out: Path) -> None:
                (out / "metadata").write_text("ok\n", encoding="utf-8")

            with patch.object(backup, "replace", side_effect=_replace_cfg), patch.object(
                backup, "_effective_cfg", return_value=cfg
            ), patch.object(
                backup, "_validate_local_storage_root", return_value=target
            ), patch.object(backup, "_ensure_cluster_tools", return_value={"mydumper": "ok"}), patch.object(
                backup, "_list_instances", return_value=[inst]
            ), patch.object(backup, "_select_instance_for_backup", return_value=inst), patch.object(
                backup, "_archive_instance_files", side_effect=archive_files
            ), patch.object(backup, "_run_mydumper", side_effect=dump), patch.object(
                backup, "_verify_dump_dir", return_value=(True, "ok", 8)
            ), patch.object(backup, "_write_storage_backup_manifest_and_index"), patch.object(
                backup, "_storage_usage", return_value=None
            ), patch.object(backup.shutil, "which", return_value="/usr/bin/tool"):
                result = backup.backup_instance_run(cfg, str(source))

            self.assertTrue(result.ok, result.message)
            final = Path(result.backup_path)
            self.assertTrue(final.is_dir())
            self.assertFalse(any(p.name.startswith(".incomplete-") for p in final.parent.iterdir()))

            loader = Mock()
            with patch.object(backup, "_effective_cfg", return_value=cfg), patch.object(
                backup, "_validate_cfg"
            ), patch.object(backup, "_validate_local_storage_root", return_value=target), patch.object(
                backup, "_list_instances", return_value=[inst]
            ), patch.object(backup, "_candidate_db", return_value=db), patch.object(
                backup, "_run_mysql_sql"
            ), patch.object(backup, "_run_myloader", loader):
                restored = backup.backup_restore(cfg, path=str(final))

            self.assertTrue(restored.ok, restored.message)
            loader.assert_called_once_with(cfg, db, final / "databases" / "tenant")

    def test_partial_failure_preserves_previous_completed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw).resolve()
            target = tmp / "target"
            source = tmp / "instance"
            target.mkdir()
            source.mkdir()
            cfg = _cfg(tmp, target)
            db = DBConfig(host="127.0.0.1", port=3306, name="tenant", user="u", password="p", table_prefix="")
            inst = SimpleNamespace(root=str(source), name="tenant", db=db, instance_uid="uid-1", primary_domain="", mautic_major=5)
            parent = target / "backup" / "host-test" / "instances" / "tenant"
            previous = parent / "20260101-000000"
            previous.mkdir(parents=True)
            (previous / ".mcd-backup.json").write_text("{}\n", encoding="utf-8")

            with patch.object(backup, "replace", side_effect=_replace_cfg), patch.object(
                backup, "_effective_cfg", return_value=cfg
            ), patch.object(
                backup, "_validate_local_storage_root", return_value=target
            ), patch.object(backup, "_ensure_cluster_tools", return_value={}), patch.object(
                backup, "_list_instances", return_value=[inst]
            ), patch.object(backup, "_select_instance_for_backup", return_value=inst), patch.object(
                backup, "_archive_instance_files", side_effect=lambda _c, _i, out: (out / "files.tar.gz")
            ), patch.object(backup, "_run_mydumper", side_effect=RuntimeError("write disconnected")), patch.object(
                backup.shutil, "which", return_value="/usr/bin/tool"
            ):
                result = backup.backup_instance_run(cfg, str(source))

            self.assertFalse(result.ok)
            self.assertTrue(previous.is_dir())
            self.assertFalse(any(p.name.startswith(".incomplete-") for p in parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
