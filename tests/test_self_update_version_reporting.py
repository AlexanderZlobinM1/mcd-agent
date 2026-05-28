from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import mcd_agent.self_update as self_update


def _cfg(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(
        state_db_path=str(Path(tmp) / "state.db"),
        mcd_update_policy="approved",
        mcd_update_channel="approved",
        mcd_auto_update_enabled=True,
    )


class SelfUpdateVersionReportingTests(unittest.TestCase):
    def test_update_status_overwrites_stale_state_versions(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state_path = Path(tmp) / "mcd-self-update.json"
            state_path.write_text(
                json.dumps(
                    {
                        "current_version": "0.9.75",
                        "running_version": "0.9.75",
                        "source_version": "0.9.75",
                        "version_mismatch": False,
                    }
                ),
                encoding="utf-8",
            )
            old_payload = self_update.agent_version_payload
            try:
                self_update.agent_version_payload = lambda: {
                    "agent_version": "0.9.37",
                    "agent_running_version": "0.9.75",
                    "agent_source_version": "0.9.37",
                    "agent_version_mismatch": True,
                }
                out = self_update.update_status(cfg)
            finally:
                self_update.agent_version_payload = old_payload

        self.assertEqual(out["current_version"], "0.9.37")
        self.assertEqual(out["running_version"], "0.9.75")
        self.assertEqual(out["source_version"], "0.9.37")
        self.assertTrue(out["version_mismatch"])

    def test_apply_update_releases_session_when_only_restart_is_needed(self) -> None:
        releases: list[dict[str, str]] = []
        restarts: list[bool] = []
        with TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            old_installed = self_update.installed_agent_version
            old_release = self_update.release_session
            old_restart = self_update._restart_service_async
            try:
                self_update.installed_agent_version = lambda: "9.9.9"
                self_update.release_session = lambda _cfg, session_id, **kw: releases.append(
                    {"session_id": session_id, **{k: str(v) for k, v in kw.items()}}
                )
                self_update._restart_service_async = lambda: restarts.append(True)
                ok, msg = self_update.apply_update(
                    cfg,
                    {
                        "status": "update",
                        "target": "9.9.9",
                        "package_url": "https://mcc.invalid/mcd-agent-9.9.9.tar.gz",
                        "session_id": "sess-1",
                    },
                )
            finally:
                self_update.installed_agent_version = old_installed
                self_update.release_session = old_release
                self_update._restart_service_async = old_restart

            state = json.loads((Path(tmp) / "mcd-self-update.json").read_text(encoding="utf-8"))

        self.assertTrue(ok)
        self.assertIn("service restart scheduled", msg)
        self.assertEqual(restarts, [True])
        self.assertEqual(releases[0]["session_id"], "sess-1")
        self.assertEqual(releases[0]["result_status"], "success")
        self.assertEqual(releases[0]["new_version"], "9.9.9")
        self.assertEqual(state["last_status"], "version_mismatch_restart")


if __name__ == "__main__":
    unittest.main()
