from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import mcd_agent.instance_delete as instance_delete


class InstanceDeleteNginxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.enabled = root / "sites-enabled"
        self.available = root / "sites-available"
        self.enabled.mkdir()
        self.available.mkdir()
        self.old_dirs = instance_delete._NGINX_DIRS
        instance_delete._NGINX_DIRS = (self.enabled, self.available)

    def tearDown(self) -> None:
        instance_delete._NGINX_DIRS = self.old_dirs
        self.tmp.cleanup()

    def test_candidates_require_exact_domain_not_shared_root(self) -> None:
        keep_conf = self.available / "keep.example.test.conf"
        delete_conf = self.available / "delete.example.test.conf"
        root = "/var/www/shared"
        keep_conf.write_text(
            f"server {{ server_name keep.example.test; root {root}; }}\n",
            encoding="utf-8",
        )
        delete_conf.write_text(
            f"server {{ server_name delete.example.test; root {root}; }}\n",
            encoding="utf-8",
        )
        (self.enabled / keep_conf.name).symlink_to(keep_conf)
        (self.enabled / delete_conf.name).symlink_to(delete_conf)

        candidates = instance_delete._nginx_candidates(Path(root), ["delete.example.test"])

        self.assertEqual(candidates, [self.enabled / delete_conf.name])

    def test_disable_symlink_preserves_available_config(self) -> None:
        conf = self.available / "site.example.test.conf"
        conf.write_text("server { server_name site.example.test; }\n", encoding="utf-8")
        enabled = self.enabled / conf.name
        enabled.symlink_to(conf)

        changed, _msg = instance_delete._disable_nginx_vhost(enabled)

        self.assertTrue(changed)
        self.assertFalse(enabled.exists() or enabled.is_symlink())
        self.assertTrue(conf.exists())

    def test_disable_regular_enabled_file_copies_to_available_first(self) -> None:
        enabled = self.enabled / "legacy.example.test.conf"
        enabled.write_text("server { server_name legacy.example.test; }\n", encoding="utf-8")

        changed, _msg = instance_delete._disable_nginx_vhost(enabled)

        self.assertTrue(changed)
        self.assertFalse(enabled.exists())
        self.assertEqual(
            (self.available / enabled.name).read_text(encoding="utf-8"),
            "server { server_name legacy.example.test; }\n",
        )


if __name__ == "__main__":
    unittest.main()
