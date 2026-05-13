from __future__ import annotations

import tempfile
import sys
import types
import unittest
from pathlib import Path

config_stub = types.ModuleType("mcd_agent.config")
config_stub.AgentConfig = object
inventory_stub = types.ModuleType("mcd_agent.inventory")
inventory_stub.InstanceInventory = object
sys.modules.setdefault("mcd_agent.config", config_stub)
sys.modules.setdefault("mcd_agent.inventory", inventory_stub)

from mcd_agent.mautic_image_install import _nginx_web_root


class MauticImageInstallNginxTests(unittest.TestCase):
    def test_composer_docroot_is_used_as_nginx_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "public_html"
            docroot = root / "docroot"
            docroot.mkdir(parents=True)
            (docroot / "index.php").write_text("<?php\n", encoding="utf-8")

            self.assertEqual(_nginx_web_root(root), docroot)

    def test_zip_root_is_used_when_no_docroot_index_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "public_html"
            root.mkdir(parents=True)
            (root / "index.php").write_text("<?php\n", encoding="utf-8")

            self.assertEqual(_nginx_web_root(root), root)


if __name__ == "__main__":
    unittest.main()
