from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcd_agent.install_type import app_bundle_dir_candidates, plugin_dir_candidates


class InstallTypePathTests(unittest.TestCase):
    def test_composer_layout_prefers_docroot_mutable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "local.php").write_text("<?php $parameters = [];\n", encoding="utf-8")
            (root / "bin").mkdir()
            (root / "bin" / "console").write_text("", encoding="utf-8")
            (root / "docroot").mkdir()

            self.assertEqual(plugin_dir_candidates(root)[0], root / "docroot" / "plugins")
            self.assertEqual(app_bundle_dir_candidates(root)[0], root / "docroot" / "app" / "bundles")

    def test_zip_layout_prefers_root_mutable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            self.assertEqual(plugin_dir_candidates(root)[0], root / "plugins")
            self.assertEqual(app_bundle_dir_candidates(root)[0], root / "app" / "bundles")


if __name__ == "__main__":
    unittest.main()
