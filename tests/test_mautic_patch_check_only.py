import tempfile
import unittest
from pathlib import Path

from mcd_agent.mautic_patch_check_only import collect_fixed_signature
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


class CheckOnlyTests(unittest.TestCase):
    def test_fixed_only_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source.php"
            record = dict(id="FIX-TEST", source_paths=["source.php"],
                gate_logic="vulnerable_all; fixed_all; mixed_or_unknown=error", gate=[
                    dict(group="vulnerable", kind="exact_count", path="source.php", needle="old", expected_count=1),
                    dict(group="fixed", kind="exact_count", path="source.php", needle="fixed", expected_count=1)])
            digest = canonical_json_sha256(record)
            source.write_bytes(b"fixed")
            before = (source.read_bytes(), source.stat().st_mtime_ns)
            result = collect_fixed_signature(root, record=record, expected_record_sha256=digest)
            self.assertEqual(result["decision"], "already")
            self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns))
            for content in (b"old", b"old fixed", b"unknown"):
                source.write_bytes(content)
                with self.assertRaises(ValueError):
                    collect_fixed_signature(root, record=record, expected_record_sha256=digest)
