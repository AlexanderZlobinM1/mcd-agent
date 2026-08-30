from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent import mautic_version_cache


class MauticVersionCacheTest(unittest.TestCase):
    def test_candidate_roots_do_not_escape_to_shared_var_www(self) -> None:
        self.assertEqual(
            mautic_version_cache._candidate_roots("/var/www/client/public_html"),
            [Path("/var/www/client/public_html"), Path("/var/www/client")],
        )

    def test_cached_version_with_wrong_major_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site" / "public_html"
            root.mkdir(parents=True)
            console = root / "bin" / "console"
            console.parent.mkdir()
            console.write_text("<?php\n", encoding="utf-8")
            generated = Path(td) / "generated"

            with (
                patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated),
                patch.object(mautic_version_cache, "_read_version_from_mcd_source", return_value="6.0.9"),
            ):
                mautic_version_cache.write_mautic_version_cache(root, "7.1.3")
                actual = mautic_version_cache.collect_mautic_version(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    expected_major=6,
                )

                self.assertEqual(actual, "6.0.9")
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "6.0.9")

    def test_migrates_legacy_cache_outside_instance_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "var" / "www" / "site" / "public_html"
            legacy = root / ".mcd" / "mautic.version"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("6.0.9\n", encoding="utf-8")
            generated = Path(td) / "opt" / "mcd" / "generated" / "mautic-version"

            with patch.object(mautic_version_cache, "_VERSION_CACHE_ROOT", generated):
                self.assertTrue(mautic_version_cache.migrate_legacy_mautic_version_cache(root))
                self.assertEqual(mautic_version_cache.read_cached_mautic_version(root), "6.0.9")
                self.assertTrue(mautic_version_cache.version_cache_path(root).is_file())

            self.assertEqual(legacy.read_text(encoding="utf-8"), "6.0.9\n")

    def test_confirmed_major_requires_runtime_and_lock_agreement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(mautic_version_cache, "descriptor_for_root", return_value=None),
                patch.object(mautic_version_cache, "_read_version_from_runtime_only", return_value="7.1.3"),
                patch.object(mautic_version_cache, "_read_major_from_composer_lock", return_value=7),
            ):
                major = mautic_version_cache.confirmed_mautic_major(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    expected_major=7,
                )

            self.assertEqual(major, 7)

    def test_confirmed_major_returns_unknown_for_missing_or_conflicting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")
            cases = (
                ("6.0.9", 7, 6),
                (None, 7, 7),
                ("7.1.3", 7, 6),
            )

            for runtime, lock, discovered in cases:
                with self.subTest(runtime=runtime, lock=lock, discovered=discovered), patch.object(
                    mautic_version_cache,
                    "descriptor_for_root",
                    return_value=None,
                ), patch.object(
                    mautic_version_cache,
                    "_read_version_from_runtime_only",
                    return_value=runtime,
                ), patch.object(
                    mautic_version_cache,
                    "_read_major_from_composer_lock",
                    return_value=lock,
                ):
                    self.assertIsNone(
                        mautic_version_cache.confirmed_mautic_major(
                            str(root),
                            "/usr/bin/php",
                            console_path=str(console),
                            expected_major=discovered,
                        )
                    )

    def test_confirmed_major_accepts_zip_install_without_composer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(mautic_version_cache, "descriptor_for_root", return_value=None),
                patch.object(mautic_version_cache, "_read_version_from_runtime_only", return_value="4.4.13"),
                patch.object(mautic_version_cache, "_read_major_from_composer_lock", return_value=None),
            ):
                major = mautic_version_cache.confirmed_mautic_major(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    local_php_path=str(root / "app" / "config" / "local.php"),
                    expected_major=4,
                )

            self.assertEqual(major, 4)

    def test_confirmed_major_rejects_mautic4_layout_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site"
            console = root / "bin" / "console"
            console.parent.mkdir(parents=True)
            console.write_text("<?php\n", encoding="utf-8")

            with (
                patch.object(mautic_version_cache, "descriptor_for_root", return_value=None),
                patch.object(mautic_version_cache, "_read_version_from_runtime_only", return_value="7.1.3"),
                patch.object(mautic_version_cache, "_read_major_from_composer_lock", return_value=7),
            ):
                major = mautic_version_cache.confirmed_mautic_major(
                    str(root),
                    "/usr/bin/php",
                    console_path=str(console),
                    local_php_path=str(root / "app" / "config" / "local.php"),
                    expected_major=7,
                )

            self.assertIsNone(major)

    def test_composer_major_supports_x_dev_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "composer.lock").write_text(
                '{"packages":[{"name":"mautic/core-lib","version":"4.3.x-dev"}]}',
                encoding="utf-8",
            )

            self.assertEqual(mautic_version_cache._read_major_from_composer_lock(root), 4)


if __name__ == "__main__":
    unittest.main()
