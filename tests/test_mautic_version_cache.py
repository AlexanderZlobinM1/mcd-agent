from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent import mautic_version_cache


class MauticVersionCacheTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
