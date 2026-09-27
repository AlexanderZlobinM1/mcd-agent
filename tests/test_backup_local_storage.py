from __future__ import annotations

import tempfile
import hashlib
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import backup
from mcd_agent import cli
from mcd_agent.models import DBConfig
from mcd_agent.mautic_json_repair import RepairAuthorizationError, issue_repair_authorization_context, validate_authorization_context, verify_backup_evidence
from mcd_agent.mautic_upgrade_contract import JSON_REPAIR_PLAN_CONTRACT, KNOWN_JSON_COLUMNS


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
            self.assertEqual(Path(result.manifest_path), final / ".mcd-backup.json")
            evidence = Path(result.authorization_manifest_path)
            self.assertEqual(
                evidence,
                Path(result.state_path).parent / "authorization-manifests" / result.sha256 / ".mcd-backup.json",
            )
            self.assertEqual(evidence.read_bytes(), Path(result.manifest_path).read_bytes())
            self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), result.sha256)
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
            output = io.StringIO()
            with patch.object(sys, "argv", [
                "mcd-cli", "backup", "--config", str(tmp / "mcd.toml"), "instance-run",
                "--root", str(source), "--json", "--remote-root-dir", "mcc/recovery",
            ]), patch.object(cli, "load_config", return_value=cfg), patch.object(
                cli, "maybe_notify_update", return_value=None,
            ), patch.object(cli, "backup_instance_run", return_value=result), patch.object(
                cli, "_push_state_after_change",
            ), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["manifest_path"], result.manifest_path)
            self.assertEqual(receipt["authorization_manifest_path"], str(evidence))
            self.assertEqual(receipt["sha256"], result.sha256)

            # Simulate a separate consumer after the transient storage mount
            # disappears; the authorization must use only durable evidence.
            offline = tmp / "offline-storage"
            target.rename(offline)
            plan = {
                "schema": JSON_REPAIR_PLAN_CONTRACT,
                "condition": "sqlstate_1253_json_collation_binary",
                "source_major": 6,
                "target_major": 7,
                "table_prefix": "ss_",
                "columns": [f"{row['table']}.{row['column']}" for row in KNOWN_JSON_COLUMNS],
                "action": "normalize_declared_json_columns",
            }
            authorization = issue_repair_authorization_context(
                root=str(source), instance_uid="uid-1", source_version="6.0.9", target_version="7.1.3",
                repair_plan_json=plan, backup_manifest_path=str(evidence),
                key_path=str(tmp / "signing.key"), output_path=str(tmp / "context.json"),
            )
            self.assertEqual(authorization["status"], "authorized")
            self.assertEqual(authorization["backup_sha256"], result.sha256)
            signed = json.loads(Path(authorization["context_path"]).read_text(encoding="utf-8"))
            validate_authorization_context(
                signed, plan=plan, instance_uid="uid-1", root=str(source),
                source_version="6.0.9", target_version="7.1.3",
                signing_key=(tmp / "signing.key").read_bytes().strip(),
            )
            self.assertEqual(signed["backup_evidence"]["manifest_path"], str(evidence))
            verify_backup_evidence(
                signed["backup_evidence"], root=str(source), instance_uid="uid-1",
            )
            evidence.write_bytes(evidence.read_bytes() + b" ")
            with self.assertRaisesRegex(RepairAuthorizationError, "backup_manifest_digest_mismatch"):
                issue_repair_authorization_context(
                    root=str(source), instance_uid="uid-1", source_version="6.0.9", target_version="7.1.3",
                    repair_plan_json=plan, backup_manifest_path=str(evidence),
                    key_path=str(tmp / "signing.key"), output_path=str(tmp / "context.json"),
                )
            evidence.unlink()
            with self.assertRaisesRegex(RepairAuthorizationError, "backup_manifest_unavailable"):
                issue_repair_authorization_context(
                    root=str(source), instance_uid="uid-1", source_version="6.0.9", target_version="7.1.3",
                    repair_plan_json=plan, backup_manifest_path=str(evidence),
                    key_path=str(tmp / "signing.key"), output_path=str(tmp / "context.json"),
                )
            offline.rename(target)

            loader = Mock()
            tar_run = Mock()
            cfg.backup_restore_apply_files = True
            with patch.object(backup, "_effective_cfg", return_value=cfg), patch.object(
                backup, "_validate_cfg"
            ), patch.object(backup, "_validate_local_storage_root", return_value=target), patch.object(
                backup, "_list_instances", return_value=[inst]
            ), patch.object(backup, "_run", tar_run), patch.object(
                backup, "_run_mysql_sql"
            ) as mysql_sql, patch.object(backup, "_run_myloader", loader):
                restored = backup.backup_restore(cfg, path=str(final))

            self.assertTrue(restored.ok, restored.message)
            loader.assert_called_once_with(cfg, db, final / "databases" / "tenant")
            mysql_sql.assert_not_called()
            self.assertEqual(tar_run.call_args.args[0][-2:], ["-C", str(source)])

    def test_instance_restore_fails_when_marker_target_is_not_managed(self) -> None:
        marker = {"dumped_instances": [{"root": "/srv/missing", "instance_uid": "missing", "database": "tenant"}]}
        with self.assertRaisesRegex(RuntimeError, "not exactly present"):
            backup._instance_restore_target(marker, [], None)

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
