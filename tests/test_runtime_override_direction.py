from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.runtime_overrides import acknowledge_desired_runtime_states, fetch_runtime_overrides, instance_desired_states, merge_instance_desired_states, overrides_fingerprint, push_runtime_overrides
from mcd_agent.config import load_config, remove_runtime_values
from mcd_agent.runtime_overrides import apply_remote_overrides, local_runtime_overrides
from mcd_agent import daemon


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

    def test_empty_authoritative_instance_state_removes_unset_override_only_for_that_uid(self) -> None:
        merged = merge_instance_desired_states(
            {
                "segment_recurring_priority_v1": {
                    "medtradcom.sales-snap.ru": {"segments": [{"id": 5, "max_interval_sec": 60}]},
                    "other.sales-snap.ru": {"segments": [{"id": 7, "max_interval_sec": 60}]},
                }
            },
            {"medtradcom.sales-snap.ru": {"runtime_overrides": {}, "revision": 2}},
        )

        self.assertEqual(
            merged["segment_recurring_priority_v1"],
            {"other.sales-snap.ru": {"segments": [{"id": 7, "max_interval_sec": 60}]}},
        )

    def test_empty_qualified_instance_state_removes_stale_bare_uid_override(self) -> None:
        merged = merge_instance_desired_states(
            {
                "segment_recurring_priority_v1": {
                    "medtradcom.sales-snap.ru": {"segments": [{"id": 5, "max_interval_sec": 60}]},
                }
            },
            {"medtradcom.sales-snap.ru@alex-personal": {"runtime_overrides": {}, "revision": 2}},
        )

        self.assertNotIn("segment_recurring_priority_v1", merged)

    def test_removed_stable_runtime_key_is_deleted_from_local_config(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text(
                '[profile]\nname = "farm-maxi"\n\n[runtime]\n'
                'segment_recurring_priority_v1 = { "medtradcom.sales-snap.ru" = { segments = [{ id = 5, max_interval_sec = 60 }] } }\n',
                encoding="utf-8",
            )

            changed_path, changed = remove_runtime_values(
                str(path),
                {"segment_recurring_priority_v1"},
            )

            self.assertTrue(changed)
            self.assertEqual(changed_path, str(path))
            self.assertNotIn("segment_recurring_priority_v1", path.read_text(encoding="utf-8"))

            rebased = load_config(str(path), allow_recover_from_mcc=False)
            applied = apply_remote_overrides(rebased, {})
            self.assertEqual(applied["config"].segment_recurring_priority_v1, {})

    def test_authoritative_instance_clear_survives_legacy_file_and_restart(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "mcd.toml"
            path.write_text(
                '[profile]\nname = "farm-maxi"\n\n[runtime]\n'
                'segment_recurring_priority_v1 = { "medtradcom.sales-snap.ru" = { segments = [{ id = 5, max_interval_sec = 60 }] } }\n',
                encoding="utf-8",
            )
            legacy = load_config(str(path), allow_recover_from_mcc=False)
            self.assertIn("segment_recurring_priority_v1", local_runtime_overrides(legacy))

            desired = merge_instance_desired_states(
                {},
                {"medtradcom.sales-snap.ru@alex-personal": {"runtime_overrides": {}, "revision": 2}},
            )
            self.assertEqual(desired, {})
            daemon._remove_absent_stable_runtime_from_config(legacy, desired)
            reloaded = load_config(str(path), allow_recover_from_mcc=False)
            effective = apply_remote_overrides(reloaded, desired)["config"]

            self.assertNotIn("segment_recurring_priority_v1", local_runtime_overrides(reloaded))
            self.assertEqual(effective.segment_recurring_priority_v1, {})
            self.assertEqual(
                load_config(str(path), allow_recover_from_mcc=False).segment_recurring_priority_v1,
                {},
            )

    def test_failed_empty_instance_ack_retries_after_restart(self) -> None:
        class MemoryStore:
            def __init__(self, values=None):
                self.values = dict(values or {})

            def get_runtime_sync(self, key):
                return self.values.get(key)

            def put_runtime_sync(self, key, value):
                self.values[key] = dict(value)

        cfg = SimpleNamespace()
        uid = "medtradcom.sales-snap.ru@alex-personal"
        rows = {uid: {"revision": 2, "runtime_overrides": {}}}
        store = MemoryStore()
        with patch(
            "mcd_agent.runtime_overrides.acknowledge_runtime_state",
            side_effect=[{"status": "error", "reason": "temporary_network_error"}, {"status": "ok"}],
        ) as acknowledge:
            failed = acknowledge_desired_runtime_states(
                cfg, store, desired_state=None, host_runtime={},
                instance_states=rows, runtime_fingerprint="aggregate",
            )
            self.assertEqual(len(failed), 1)
            self.assertNotIn("mcc_runtime_acknowledged", store.values)

            restarted = MemoryStore(store.values)
            self.assertEqual(
                acknowledge_desired_runtime_states(
                    cfg, restarted, desired_state=None, host_runtime={},
                    instance_states=rows, runtime_fingerprint="aggregate",
                ),
                [],
            )
            self.assertEqual(
                restarted.values["mcc_runtime_acknowledged"]["instances"][uid],
                {"scope_key": uid, "revision": 2, "content_sha256": overrides_fingerprint({})},
            )
            acknowledge_desired_runtime_states(
                cfg, restarted, desired_state=None, host_runtime={},
                instance_states=rows, runtime_fingerprint="aggregate",
            )
            self.assertEqual(acknowledge.call_count, 2)
            self.assertEqual(acknowledge.call_args.kwargs["revision"], 2)
            self.assertEqual(
                acknowledge.call_args.kwargs["observed"]["content_sha256"],
                overrides_fingerprint({}),
            )

    def test_new_host_revision_is_acked_even_when_value_is_unchanged(self) -> None:
        store = SimpleNamespace(values={})
        store.get_runtime_sync = lambda key: store.values.get(key)
        store.put_runtime_sync = lambda key, value: store.values.__setitem__(key, dict(value))
        cfg = SimpleNamespace()
        with patch("mcd_agent.runtime_overrides.acknowledge_runtime_state", return_value={"status": "ok"}) as acknowledge:
            for revision in (3, 4, 4):
                acknowledge_desired_runtime_states(
                    cfg, store,
                    desired_state={"scope_key": "test-host", "revision": revision},
                    host_runtime={}, instance_states={}, runtime_fingerprint="aggregate",
                )
        self.assertEqual(acknowledge.call_count, 2)
        self.assertEqual(store.values["mcc_runtime_acknowledged"]["host"]["revision"], 4)


if __name__ == "__main__":
    unittest.main()
