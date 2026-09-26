from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from mcd_agent import mautic_patch_backup as backup
from mcd_agent import mautic_patch_plan_v3 as executor
from mcd_agent.mautic_json_repair import RepairAuthorizationError, _canonical
from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from mcd_agent.mautic_upgrade_contract import inspect_json_schema_repair


class PatchBackupAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.uid = "fixture-instance-001"
        self.now = 1800000000
        fixture = Path(__file__).parents[1] / "mcd_agent/contracts/fixtures/mautic-patch-resolution-v1.json"
        self.plan = json.loads(fixture.read_text())["resolve_response"]["plan"]
        self.plan.update(source_version="6.0.6", target_version="7.2.0", trigger="upgrade_lifecycle", phase="dependency_update_preflight")
        self.plan["patches"][0]["triggers"] = ["upgrade_lifecycle"]
        self.plan["patches"][0]["phases"] = ["post_source_install"]
        directory = self.root / "backup"
        directory.mkdir()
        manifest = directory / ".mcd-backup.json"
        completed = datetime.fromtimestamp(self.now - 60, timezone.utc).isoformat()
        marker = {"status": "ok", "backup_id": self.uid + ":fixture", "ts_utc": completed,
                  "bytes_written": 123, "dumped_instances": [{"instance_uid": self.uid, "root": str(self.root)}]}
        raw = json.dumps(marker).encode()
        manifest.write_bytes(raw)
        self.key = b"fixture-only-signing-key-32-bytes!"
        self.context = {"schema": backup.SCHEMA, "instance_uid": self.uid, "root": str(self.root),
                        "source_version": self.plan["source_version"], "target_version": self.plan["target_version"],
                        "run_id": self.plan["run_id"], "plan_sha256": canonical_json_sha256(self.plan),
                        "backup_evidence": {"backup_id": marker["backup_id"], "backup_path": str(directory),
                                            "manifest_path": str(manifest), "sha256": hashlib.sha256(raw).hexdigest(),
                                            "completed_at": completed, "bytes_written": 123},
                        "issued_at": self.now - 10, "expires_at": self.now + 60, "nonce": "a" * 32}
        self._sign()

    def tearDown(self):
        self.tmp.cleanup()

    def _sign(self):
        value = {k: v for k, v in self.context.items() if k != "signature"}
        self.context["signature"] = hmac.new(self.key, _canonical(value), hashlib.sha256).hexdigest()

    def _validate(self):
        return backup.validate(self.context, plan=self.plan, instance_uid=self.uid,
                               root=str(self.root), signing_key=self.key, now=self.now)

    def test_authenticates_separate_backup_without_changing_plan(self):
        digest = canonical_json_sha256(self.plan)
        self.assertEqual(self._validate()["bytes_written"], 123)
        self.assertEqual(canonical_json_sha256(self.plan), digest)
        self.assertTrue(backup.required(self.plan))
        same_major = dict(self.plan, source_version="7.1.3")
        self.assertFalse(backup.required(same_major))

    def test_unsigned_tampering_is_rejected(self):
        self.context["backup_evidence"]["bytes_written"] = 999
        with self.assertRaisesRegex(RepairAuthorizationError, "signature_invalid"):
            self._validate()

    def test_signed_wrong_plan_or_run_and_expired_context_are_rejected(self):
        for key, value in (("run_id", "different-run"), ("plan_sha256", "0" * 64)):
            original = self.context[key]
            self.context[key] = value
            self._sign()
            with self.assertRaisesRegex(RepairAuthorizationError, "binding_mismatch"):
                self._validate()
            self.context[key] = original
        self.context["expires_at"] = self.now
        self._sign()
        with self.assertRaisesRegex(RepairAuthorizationError, "expired"):
            self._validate()

    def test_manifest_drift_is_rejected_even_with_valid_signature(self):
        Path(self.context["backup_evidence"]["manifest_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(RepairAuthorizationError, "digest_mismatch"):
            self._validate()

    def test_same_major_json_repair_preflight_does_not_require_external_backup(self):
        result = inspect_json_schema_repair(root=str(self.root), current_version="7.1.3", target_version="7.2.0")
        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result["backup_prerequisite"]["required"])

    def test_published_attestation_vector_has_exact_cross_component_hash_bytes(self):
        import base64
        fixture = Path(__file__).parents[1] / "mcd_agent/contracts/fixtures/mautic-patch-backup-attestation-v1.json"
        vector = json.loads(fixture.read_text())
        envelope = dict(vector["attestation"])
        signature = envelope.pop("signature")
        key = bytes.fromhex(vector["fixture_only_key_hex"])
        self.assertEqual(hmac.new(key, _canonical(envelope), hashlib.sha256).hexdigest(), signature)
        self.assertEqual(canonical_json_sha256(vector["plan"]), envelope["plan_sha256"])
        raw = base64.b64decode(vector["manifest_bytes_base64"], validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), envelope["backup_evidence"]["sha256"])

    def test_phase_execution_preserves_plan_and_preflight_is_read_only(self):
        path = self.root / "docroot/app/fixture.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"before\n")
        digest = canonical_json_sha256(self.plan)
        executor.atomic_preflight(str(self.root), self.plan)
        self.assertEqual(path.read_bytes(), b"before\n")
        executor.execute(str(self.root), self.plan, phase="before_doctrine_migrations")
        self.assertEqual(path.read_bytes(), b"before\n")
        executor.execute(str(self.root), self.plan, phase="post_source_install")
        self.assertEqual(path.read_bytes(), b"after\n")
        self.assertEqual(canonical_json_sha256(self.plan), digest)
        executor.execute(str(self.root), dict(self.plan, operation="rollback"))
        self.assertEqual(path.read_bytes(), b"before\n")
