from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        keep_conf = self.available / "keep.sales-snap.com.conf"
        delete_conf = self.available / "delete.sales-snap.com.conf"
        root = "/var/www/shared"
        keep_conf.write_text(
            f"server {{ server_name keep.sales-snap.com; root {root}; }}\n",
            encoding="utf-8",
        )
        delete_conf.write_text(
            f"server {{ server_name delete.sales-snap.com; root {root}; }}\n",
            encoding="utf-8",
        )
        (self.enabled / keep_conf.name).symlink_to(keep_conf)
        (self.enabled / delete_conf.name).symlink_to(delete_conf)

        candidates = instance_delete._nginx_candidates(Path(root), ["delete.sales-snap.com"])

        self.assertEqual(candidates, [self.enabled / delete_conf.name])

    def test_disable_symlink_preserves_available_config(self) -> None:
        conf = self.available / "site.sales-snap.com.conf"
        conf.write_text("server { server_name site.sales-snap.com; }\n", encoding="utf-8")
        enabled = self.enabled / conf.name
        enabled.symlink_to(conf)

        changed, _msg = instance_delete._disable_nginx_vhost(enabled)

        self.assertTrue(changed)
        self.assertFalse(enabled.exists() or enabled.is_symlink())
        self.assertTrue(conf.exists())

    def test_disable_regular_enabled_file_copies_to_available_first(self) -> None:
        enabled = self.enabled / "legacy.sales-snap.com.conf"
        enabled.write_text("server { server_name legacy.sales-snap.com; }\n", encoding="utf-8")

        changed, _msg = instance_delete._disable_nginx_vhost(enabled)

        self.assertTrue(changed)
        self.assertFalse(enabled.exists())
        self.assertEqual(
            (self.available / enabled.name).read_text(encoding="utf-8"),
            "server { server_name legacy.sales-snap.com; }\n",
        )

    def test_remove_instance_root_retries_after_directory_not_empty(self) -> None:
        root = Path(self.tmp.name) / "public_html"
        (root / ".mcd").mkdir(parents=True)
        (root / ".mcd" / "mautic.version").write_text("7.1.2\n", encoding="utf-8")
        original_rmtree = shutil.rmtree
        calls = 0

        def flaky_rmtree(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("[Errno 39] Directory not empty")
            original_rmtree(path)

        with patch.object(instance_delete.shutil, "rmtree", side_effect=flaky_rmtree):
            instance_delete._remove_instance_root(root, attempts=3, sleep_sec=0)

        self.assertGreaterEqual(calls, 2)
        self.assertFalse(root.exists())

    def test_remove_instance_root_reports_remaining_entries(self) -> None:
        root = Path(self.tmp.name) / "public_html"
        (root / ".mcd").mkdir(parents=True)
        (root / ".mcd" / "mautic.version").write_text("7.1.2\n", encoding="utf-8")

        with patch.object(instance_delete.shutil, "rmtree", side_effect=OSError("[Errno 39] Directory not empty")):
            with self.assertRaisesRegex(RuntimeError, r"mautic\.version"):
                instance_delete._remove_instance_root(root, attempts=2, sleep_sec=0)


if __name__ == "__main__":
    unittest.main()
