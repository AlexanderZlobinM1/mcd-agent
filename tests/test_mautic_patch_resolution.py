from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcd_agent import mautic_patch_resolution as resolution


FIXTURE = Path(__file__).parents[1] / "mcd_agent" / "contracts" / "fixtures" / "mautic-patch-resolution-v1.json"


class _Response:
    def __init__(self, value):
        self.raw = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.raw


class MauticPatchResolutionTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.config = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="test-token")
        self.install = SimpleNamespace(instance_uid="fixture-instance-001")

    def _identity(self, _config):
        return {
            "effective_mcc_host_name": "fixture-mcc-host",
            "effective_hostname": "fixture.example",
        }

    def _resolve(self, response):
        with (
            patch.object(resolution, "resolve_agent_identity", side_effect=self._identity),
            patch.object(resolution.urllib.request, "urlopen", return_value=_Response(response)) as urlopen,
        ):
            result = resolution.resolve_plan(
                self.config,
                self.install,
                trigger="daemon_reconcile",
                phase="before_plugin_reload",
                operation="apply",
                observed_version="7.2.0",
                observed_major=7,
                install_type="composer",
                run_id="fixture-runtime-001",
            )
        return result, urlopen

    def test_resolves_fixture_and_authenticates_request(self):
        result, urlopen = self._resolve(self.fixture["resolve_response"])
        self.assertEqual(result["status"], "selected")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://mcc.example/api/v1/agent/mautic-patches/resolve")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        payload = json.loads(request.data)
        self.assertEqual(payload["schema"], "mcc-mautic-patch-resolve-v1")
        self.assertEqual(payload["mcc_host_name"], "fixture-mcc-host")
        self.assertEqual(payload["agent_version"], "1.2.63")
        self.assertIsNone(payload["target_version"])

    def test_rejects_plan_hash_mismatch(self):
        response = json.loads(json.dumps(self.fixture["resolve_response"]))
        response["plan_sha256"] = "0" * 64
        with self.assertRaisesRegex(resolution.MauticPatchResolutionError, "plan_sha256_mismatch"):
            self._resolve(response)

    def test_requires_rollback_run_id(self):
        with self.assertRaisesRegex(resolution.MauticPatchResolutionError, "rollback_run_id_required"):
            resolution.resolve_plan(
                self.config,
                self.install,
                trigger="operator_action",
                phase="legacy_runtime_plugin_repair",
                operation="rollback",
                observed_version="7.2.0",
                install_type="composer",
            )

    def test_unknown_version_requires_confirmed_major(self):
        with self.assertRaisesRegex(resolution.MauticPatchResolutionError, "unknown_version_requires_confirmed_major"):
            resolution.resolve_plan(
                self.config,
                self.install,
                trigger="daemon_reconcile",
                phase="before_plugin_reload",
                operation="status",
                observed_version=None,
                install_type="composer",
            )

    def test_reports_evidence_with_server_resolved_host_id(self):
        response = {"schema": "mcc-mautic-patch-evidence-accepted-v1", "evidence_id": "evidence-1", "status": "accepted"}
        with patch.object(resolution.urllib.request, "urlopen", return_value=_Response(response)) as urlopen:
            result = resolution.report_evidence(
                self.config,
                instance_uid="fixture-instance-001",
                mcc_host_name="fixture-mcc-host",
                hostname="fixture.example",
                host_id="550e8400-e29b-41d4-a716-446655440000",
                catalog_revision="fixture-catalog-rev-1",
                catalog_sha256="c" * 64,
                plan_sha256="4b4340be8c3bd6242ffca8eaf6ecaac4cefcdcc55a77f9cc31c2d0bb3399a2b2",
                trigger="daemon_reconcile",
                phase="before_plugin_reload",
                run_id="fixture-runtime-001",
                evidence={"schema": "mcd-mautic-patch-evidence-v1", "status": "success"},
            )
        self.assertEqual(result["evidence_id"], "evidence-1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://mcc.example/api/v1/agent/mautic-patches/evidence")
        self.assertEqual(json.loads(request.data)["host_id"], "550e8400-e29b-41d4-a716-446655440000")


if __name__ == "__main__":
    unittest.main()
