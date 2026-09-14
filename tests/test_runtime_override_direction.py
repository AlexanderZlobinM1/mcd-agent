from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.runtime_overrides import fetch_runtime_overrides, instance_desired_states, merge_instance_desired_states, push_runtime_overrides


class RuntimeOverrideDirectionTests(unittest.TestCase):
    def test_poll_includes_canonical_host_qualified_instance_uid(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        identity = {
            "effective_hostname": "MauticFarm-02",
            "effective_mcc_host_name": "MauticFarm-02",
            "local_hostname": "MauticFarm-02",
            "configured_host_name": "",
        }
        with patch("mcd_agent.runtime_overrides.resolve_agent_identity", return_value=identity), patch(
            "mcd_agent.runtime_overrides._post_json", return_value={"status": "ok"}
        ) as post:
            fetch_runtime_overrides(cfg, instance_uids=["electronic.sales-snap.com"])

        payload = post.call_args.args[1]
        self.assertEqual(
            payload["instance_uids"],
            ["electronic.sales-snap.com", "electronic.sales-snap.com@MauticFarm-02"],
        )

    def test_local_runtime_push_targets_canonical_desired_state_when_requested(self) -> None:
        cfg = SimpleNamespace(mcc_url="https://mcc.example", mcc_token="token")
        identity = {
            "effective_hostname": "host-1",
            "effective_mcc_host_name": "host-1",
            "local_hostname": "host-1",
            "configured_host_name": "",
        }
        with patch("mcd_agent.runtime_overrides.resolve_agent_identity", return_value=identity), patch(
            "mcd_agent.runtime_overrides._post_json", return_value={"status": "ok"}
        ) as post:
            result = push_runtime_overrides(
                cfg,
                {"segment_whitelist_instance_settings": {"/var/www/app": {"segment_whitelist": [86]}}},
                target="desired",
            )

        self.assertEqual(result["status"], "ok")
        payload = post.call_args.args[1]
        self.assertEqual(payload["target"], "desired")
        self.assertEqual(payload["push_mode"], "replace")
        self.assertEqual(payload["desired_state_protocol"], 1)

    def test_instance_state_uses_uid_and_reapplies_after_root_move(self) -> None:
        inst = SimpleNamespace(
            instance_uid="app.sales-snap.com",
            root="/var/www/old/public_html",
            name="app",
            primary_domain="app.sales-snap.com",
            domains=["app.sales-snap.com"],
        )
        states = instance_desired_states(
            {
                "segment_whitelist_instance_settings": {
                    "/var/www/old/public_html": {"segment_whitelist": [86]},
                },
                "segment_recurring_priority_v1": {
                    "/var/www/old/public_html": {"segments": [{"id": 86, "max_interval_sec": 60}]},
                },
            },
            [inst],
        )
        self.assertEqual(states["app.sales-snap.com"]["segment_whitelist_instance_settings"]["segment_whitelist"], [86])
        self.assertEqual(states["app.sales-snap.com"]["segment_recurring_priority_v1"]["segments"][0]["id"], 86)
        merged = merge_instance_desired_states(
            {},
            {
                "app.sales-snap.com": {
                    "runtime_overrides": states["app.sales-snap.com"],
                    "revision": 1,
                }
            },
        )
        self.assertEqual(
            merged["segment_whitelist_instance_settings"]["app.sales-snap.com"]["segment_whitelist"],
            [86],
        )
        self.assertEqual(
            merged["segment_recurring_priority_v1"]["app.sales-snap.com"]["segments"][0]["max_interval_sec"],
            60,
        )

    def test_canonical_desired_state_matches_legacy_local_inventory_uid_and_acks_canonical_uid(self) -> None:
        inst = SimpleNamespace(
            instance_uid="electronic.sales-snap.com",
            root="/var/www/electronic/public_html",
            name="electronic.sales-snap.com",
            primary_domain="electronic.sales-snap.com",
            domains=["electronic.sales-snap.com"],
        )
        canonical_uid = "electronic.sales-snap.com@MauticFarm-02"
        states = instance_desired_states(
            {
                "segment_recurring_priority_v1": {
                    canonical_uid: {"segments": [{"id": 86, "max_interval_sec": 60}]},
                }
            },
            [inst],
        )

        self.assertEqual(list(states), [canonical_uid])
        self.assertEqual(states[canonical_uid]["segment_recurring_priority_v1"]["segments"][0]["id"], 86)


if __name__ == "__main__":
    unittest.main()
