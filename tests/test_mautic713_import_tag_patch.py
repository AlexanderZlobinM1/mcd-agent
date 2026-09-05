from __future__ import annotations

import tempfile
import unittest
import json
import fcntl
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.mautic713_import_tag_patch import ensure_patch, patch_status, revert_patch, reconcile_import_tag_patch
from mcd_agent.models import MauticInstall


_SOURCE = """<?php
elseif (!$leadTags->contains($foundTags[$tag])) {
    $tagToBeAdded = $foundTags[$tag];
}
"""


class Mautic713ImportTagPatchTests(unittest.TestCase):
    def _install(self, root: Path) -> MauticInstall:
        return MauticInstall(instance_uid="i", name="i", root=str(root), console_path=str(root / "bin/console"))

    def _lead_model(self, root: Path) -> Path:
        path = root / "docroot/app/bundles/LeadBundle/Model/LeadModel.php"
        path.parent.mkdir(parents=True)
        path.write_text(_SOURCE, encoding="utf-8")
        return path

    def test_apply_is_idempotent_and_revert_restores_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.1.3"):
            root = Path(td)
            path = self._lead_model(root)
            install = self._install(root)

            self.assertEqual(ensure_patch(install)["status"], "patched")
            self.assertEqual(ensure_patch(install)["status"], "already")
            self.assertIn("getReference(Tag::class", path.read_text(encoding="utf-8"))
            self.assertEqual(patch_status(install)["status"], "patched")

            result = revert_patch(install)
            self.assertEqual(result["status"], "reverted")
            self.assertEqual(path.read_text(encoding="utf-8"), _SOURCE)

    def test_rejects_other_versions_without_touching_core(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="8.0.0"):
            root = Path(td)
            path = self._lead_model(root)
            result = ensure_patch(self._install(root))
            self.assertEqual(result["status"], "skip")
            self.assertEqual(result["reason"], "version_out_of_scope")
            self.assertEqual(path.read_text(encoding="utf-8"), _SOURCE)

    def test_revert_refuses_changed_patched_file(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.1.3"):
            root = Path(td)
            path = self._lead_model(root)
            install = self._install(root)
            self.assertEqual(ensure_patch(install)["status"], "patched")
            path.write_text(path.read_text(encoding="utf-8") + "// external change\n", encoding="utf-8")
            result = revert_patch(install)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["reason"], "patched_file_changed")

    def test_all_reviewed_minor_lines_and_supported_layouts(self) -> None:
        for version in ("7.0.0", "7.0.2", "7.1.2", "7.1.3", "7.2.0"):
            for webroot in ("", "docroot", "public"):
                with self.subTest(version=version, layout=webroot), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    app = root / webroot / "app"
                    path = app / "bundles/LeadBundle/Model/LeadModel.php"
                    path.parent.mkdir(parents=True)
                    path.write_text(_SOURCE)
                    (app / "release_metadata.json").write_text(json.dumps({"version": version}))
                    self.assertEqual(ensure_patch(self._install(root))["status"], "patched")
                    self.assertEqual(revert_patch(self._install(root))["status"], "reverted")

    def test_shipped_release_metadata_overrides_stale_local_version(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.1.3"):
            root = Path(td)
            path = self._lead_model(root)
            metadata = path.parents[3] / "release_metadata.json"
            for version in ("4.4.13", "6.0.9", "7.3.0", "8.0.0", "7.2.0-rc", "unknown"):
                metadata.write_text(json.dumps({"version": version}))
                self.assertEqual(ensure_patch(self._install(root))["status"], "skip")
                self.assertEqual(path.read_text(), _SOURCE)
            metadata.write_text("broken json")
            self.assertEqual(ensure_patch(self._install(root))["reason"], "invalid_release_metadata")

    def test_reconcile_defaults_on_and_preserves_maintenance_and_docker(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.2.0"):
            root = Path(td)
            path = self._lead_model(root)
            install = self._install(root)
            pause = root / "scheduler.pause"
            cfg = SimpleNamespace(scheduler_pause_flag_path=str(pause), profile="passive")
            pause.touch()
            self.assertEqual(reconcile_import_tag_patch(cfg, [install]), [])
            self.assertEqual(path.read_text(), _SOURCE)
            pause.unlink()
            install.runtime = "docker"
            self.assertEqual(reconcile_import_tag_patch(cfg, [install])[0]["reason"], "runtime_not_host")
            self.assertEqual(path.read_text(), _SOURCE)
            install.runtime = "host"
            self.assertEqual(reconcile_import_tag_patch(cfg, [install])[0]["status"], "patched")
            self.assertEqual(reconcile_import_tag_patch(cfg, [install])[0]["status"], "already")

    def test_upgrade_overwrite_refreshes_backup_for_current_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.1.3"):
            root = Path(td)
            path = self._lead_model(root)
            install = self._install(root)
            self.assertEqual(ensure_patch(install)["status"], "patched")
            upgraded = _SOURCE + "// newer upstream core\n"
            path.write_text(upgraded)
            (path.parents[3] / "release_metadata.json").write_text('{"version":"7.2.0"}')
            self.assertEqual(ensure_patch(install)["status"], "patched")
            self.assertEqual(revert_patch(install)["status"], "reverted")
            self.assertEqual(path.read_text(), upgraded)

    def test_ambiguous_source_and_upstream_fix_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.2.0"):
            root = Path(td)
            path = self._lead_model(root)
            install = self._install(root)
            for source in (_SOURCE * 2, "<?php\n$tagToBeAdded = $foundTags[$tag];\n", "<?php // revised upstream code\n"):
                path.write_text(source)
                self.assertEqual(ensure_patch(install)["status"], "skip")
                self.assertEqual(path.read_text(), source)
            fixed = _SOURCE.replace("$tagToBeAdded = $foundTags[$tag];", "$tagToBeAdded = $this->em->getReference(Tag::class, $foundTags[$tag]->getId());")
            path.write_text(fixed)
            self.assertEqual(ensure_patch(install)["status"], "already")
            self.assertEqual(revert_patch(install)["status"], "clean")
            self.assertEqual(path.read_text(), fixed)

    def test_write_error_is_reported_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.2.0"):
            root = Path(td)
            path = self._lead_model(root)
            with patch("mcd_agent.mautic713_import_tag_patch._write", side_effect=PermissionError("read only")):
                self.assertEqual(ensure_patch(self._install(root))["status"], "error")
            self.assertEqual(path.read_text(), _SOURCE)

    def test_concurrent_patch_does_not_block_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.2.0"):
            root = Path(td)
            path = self._lead_model(root)
            with path.with_name(path.name + ".mcd-import-tag.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(ensure_patch(self._install(root))["reason"], "patch_busy")
            self.assertEqual(path.read_text(), _SOURCE)

    def test_upgrade_restores_before_replace_and_repatches_before_resume(self) -> None:
        from mcd_agent.mautic_upgrade import run_upgrade_apply

        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            path = self._lead_model(root)
            metadata = path.parents[3] / "release_metadata.json"
            metadata.write_text('{"version":"7.1.3"}')
            install = self._install(root)
            install.mautic_major = 7
            self.assertEqual(ensure_patch(install)["status"], "patched")
            upgraded = _SOURCE + "// Mautic 7.2.0\n"

            def apply_zip(*args, **kwargs):
                self.assertEqual(path.read_text(), _SOURCE)
                path.write_text(upgraded)
                metadata.write_text('{"version":"7.2.0"}')

            def resume(*args, **kwargs):
                self.assertIn("getReference(Tag::class", path.read_text())

            prefix = "mcd_agent.mautic_upgrade."
            stack.enter_context(patch(prefix + "_require_release_approval"))
            for name in ("_enter_upgrade_maintenance", "_pre_upgrade_permissions_check", "ensure_mailer_packages_for_sender_config", "ensure_amazon_mailer_for_bundles", "installed_required_bundles", "_post_upgrade_verify", "_write_upgrade_version_cache"):
                stack.enter_context(patch(prefix + name))
            stack.enter_context(patch(prefix + "_pick_install_record", return_value=install))
            stack.enter_context(patch(prefix + "_read_current_version", side_effect=["7.1.3", "7.2.0"]))
            stack.enter_context(patch(prefix + "_apply_zip", side_effect=apply_zip))
            stack.enter_context(patch(prefix + "_exit_upgrade_maintenance", side_effect=resume))
            cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data")
            self.assertEqual(run_upgrade_apply(config=cfg, root=str(root), mode="zip", yes=True, do_backup=False, with_system_upgrade=False, target_override="7.2.0", allow_minor=True), 0)
            self.assertEqual(revert_patch(install)["status"], "reverted")
            self.assertEqual(path.read_text(), upgraded)


if __name__ == "__main__":
    unittest.main()
