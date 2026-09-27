import copy
import unittest
import hashlib
import json

from mcd_agent.mautic_target_patch_verification import validate_snapshot, BINDINGS
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


class VerificationSnapshotTests(unittest.TestCase):
    def fixture(self):
        record = dict(id="FIX-TEST", source_paths=["source.php"])
        catalog = json.dumps([record], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        value = dict(schema="mcc-mautic-target-patch-verification-plan-v1",
            job_id="job1", run_id="run1", prepared_target_id="p" * 32,
            original_patch_plan_sha256="a" * 64, catalog_revision=hashlib.sha1(catalog).hexdigest(),
            catalog_sha256=hashlib.sha256(catalog).hexdigest(), target_artifact_sha256="c" * 64,
            execution_context=dict(host_id="00000000-0000-0000-0000-000000000001",
                instance_uid="wire", application_root="/var/www/fixture", table_prefix="ss_"),
            identity_association=dict(schema="mcd-instance-identity-association-v1",
                host_id="00000000-0000-0000-0000-000000000001", wire_instance_uid="wire",
                local_instance_uid="local", application_root="/var/www/fixture", table_prefix="ss_"),
            root_mapping=dict(schema="mcd-mautic-root-mapping-v1", project_root="/var/www/fixture",
                application_root_relative=".", console_relative_path="bin/console"),
            excluded_records=[dict(id="FIX-TEST", record_sha256=canonical_json_sha256(record), record=record)])
        return value, {k: value[k] for k in BINDINGS}, catalog

    def test_immutable_bindings_and_catalog_record(self):
        value, binding, catalog = self.fixture()
        self.assertEqual(validate_snapshot(value, expected_binding=binding,
            catalog_bytes=catalog, original_patch_ids=set()), canonical_json_sha256(value))
        for key in ("target_artifact_sha256", "original_patch_plan_sha256", "catalog_sha256"):
            changed = copy.deepcopy(value)
            changed[key] = "d" * 64
            with self.assertRaises(ValueError):
                validate_snapshot(changed, expected_binding=binding, catalog_bytes=catalog, original_patch_ids=set())
        changed = copy.deepcopy(value)
        changed["excluded_records"][0]["record"]["source_paths"] = ["other.php"]
        with self.assertRaises(ValueError):
            validate_snapshot(changed, expected_binding=binding, catalog_bytes=catalog, original_patch_ids=set())

    def test_duplicates_and_unknown_fields_rejected(self):
        value, binding, catalog = self.fixture()
        value["excluded_records"] *= 2
        with self.assertRaises(ValueError):
            validate_snapshot(value, expected_binding=binding, catalog_bytes=catalog, original_patch_ids=set())
        value["extra"] = True
        with self.assertRaises(ValueError):
            validate_snapshot(value, expected_binding=binding, catalog_bytes=catalog, original_patch_ids=set())
