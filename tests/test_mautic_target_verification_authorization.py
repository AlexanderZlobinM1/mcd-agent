import json
import time
import unittest
from unittest.mock import MagicMock, patch

from mcd_agent.mautic_target_verification_authorization import authorize_target_verification, BINDINGS, PURPOSE
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


class TargetAdmissionTests(unittest.TestCase):
    def test_exact_online_echo_and_drift_rejection(self):
        snapshot = {key: "fixture" for key in BINDINGS}
        now = int(time.time())
        claims = dict(snapshot, schema="mcc-mautic-target-patch-verification-authorization-v1",
            purpose=PURPOSE, verification_plan_sha256=canonical_json_sha256(snapshot),
            issued_at=now, expires_at=now + 180, nonce="a" * 32, signature="b" * 64)
        accepted = dict(snapshot, schema="mcc-mautic-target-patch-verification-admission-v1",
            status="accepted", purpose=PURPOSE, verification_plan_sha256=canonical_json_sha256(snapshot),
            authorization_context_sha256=canonical_json_sha256(claims), issued_at=now, expires_at=now + 180)
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(accepted).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            result = authorize_target_verification(snapshot=snapshot, authorization_context=claims,
                mcc_url="https://mcc.example", token="private-token")
            self.assertEqual(result, accepted)
            accepted["expires_at"] += 1
            response.read.return_value = json.dumps(accepted).encode()
            with self.assertRaisesRegex(ValueError, "authorization_rejected"):
                authorize_target_verification(snapshot=snapshot, authorization_context=claims,
                    mcc_url="https://mcc.example", token="private-token")
        self.assertEqual(opener.open.call_count, 2)
