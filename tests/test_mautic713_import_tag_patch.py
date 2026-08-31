from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent.mautic713_import_tag_patch import ensure_patch, patch_status, revert_patch
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
        with tempfile.TemporaryDirectory() as td, patch("mcd_agent.mautic713_import_tag_patch.detect_mautic_version", return_value="7.1.4"):
            root = Path(td)
            path = self._lead_model(root)
            result = ensure_patch(self._install(root))
            self.assertEqual(result["status"], "skip")
            self.assertEqual(result["reason"], "version_not_7_1_3")
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


if __name__ == "__main__":
    unittest.main()
