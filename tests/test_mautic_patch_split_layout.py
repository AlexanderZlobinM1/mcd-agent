import tempfile
import unittest
from pathlib import Path

from mcd_agent.mautic_patch_stage import application_root
from mcd_agent.mautic_patch_plan_v3 import PatchPlanV3Error


class SplitComposerLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def anchors(self, root, console=True):
        (root / "app").mkdir(parents=True)
        (root / "plugins").mkdir()
        if console:
            (root / "bin").mkdir()
            (root / "bin/console").write_bytes(b"fixture")

    def test_split_project_resolves_docroot(self):
        self.anchors(self.root / "docroot", console=False)
        (self.root / "bin").mkdir()
        (self.root / "bin/console").write_bytes(b"fixture")
        self.assertEqual(application_root(self.root), self.root / "docroot")
        self.assertEqual(application_root(self.root / "docroot"), self.root / "docroot")

    def test_direct_public_split_layout(self):
        self.anchors(self.root / "public", console=False)
        (self.root / "bin").mkdir()
        (self.root / "bin/console").write_bytes(b"fixture")
        self.assertEqual(application_root(self.root / "public"), self.root / "public")

    def test_root_layout_unchanged(self):
        self.anchors(self.root)
        self.assertEqual(application_root(self.root), self.root)

    def test_ambiguous_layout_rejected(self):
        self.anchors(self.root)
        self.anchors(self.root / "docroot", console=False)
        with self.assertRaises(PatchPlanV3Error):
            application_root(self.root)
        with self.assertRaises(PatchPlanV3Error):
            application_root(self.root / "docroot")

    def test_split_console_symlink_rejected(self):
        self.anchors(self.root / "docroot", console=False)
        (self.root / "bin").mkdir()
        (self.root / "console").write_bytes(b"fixture")
        (self.root / "bin/console").symlink_to(self.root / "console")
        with self.assertRaises(PatchPlanV3Error):
            application_root(self.root)
