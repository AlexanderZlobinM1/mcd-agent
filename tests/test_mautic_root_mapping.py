import tempfile
import unittest
from pathlib import Path

from mcd_agent.mautic_root_mapping import discover_root_mapping


class RootMappingTests(unittest.TestCase):
    def test_split_entry_points_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("docroot/app", "docroot/plugins", "bin"):
                (root / name).mkdir(parents=True)
            (root / "bin/console").write_bytes(b"fixture")
            expected = dict(schema="mcd-mautic-root-mapping-v1", project_root=str(root),
                application_root_relative="docroot", console_relative_path="bin/console")
            self.assertEqual(discover_root_mapping(root), expected)
            self.assertEqual(discover_root_mapping(root / "docroot"), expected)

    def test_two_consoles_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("docroot/app", "docroot/plugins", "docroot/bin", "bin"):
                (root / name).mkdir(parents=True)
            for name in ("bin/console", "docroot/bin/console"):
                (root / name).write_bytes(b"fixture")
            with self.assertRaises(ValueError):
                discover_root_mapping(root)
