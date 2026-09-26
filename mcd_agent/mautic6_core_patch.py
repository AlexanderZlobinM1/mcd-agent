"""Compatibility entry points for catalog-managed runtime remediation."""
from __future__ import annotations
from typing import Any
from mcd_agent.mautic_patch_runtime import run
from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only


def detect_mautic_version(install) -> str | None:
    return read_mautic_version_evidence_read_only(install.root).get("version")


def should_apply_m6_plugin_update_metadata_patch(install, *, policy="required", version_min=None, version_max=None, apply_if_version_unknown=False):
    version = detect_mautic_version(install)
    if policy != "required":
        return {"apply": False, "reason": "policy_off", "version": version}
    if version is None and not apply_if_version_unknown:
        return {"apply": False, "reason": "unknown_version_policy_off", "version": version}
    if version is not None and version.split(".")[0] != "6":
        return {"apply": False, "reason": "not_mautic_6", "version": version}
    return {"apply": True, "reason": "catalog_selection_required", "version": version}


def patch_status(install, config: Any = None):
    return run(config, install, phase="before_plugin_reload", operation="status", trigger="operator_action")


def ensure_m6_plugin_update_metadata_patch(install, config: Any = None, *, trigger="daemon_reconcile"):
    if config is not None and getattr(config, "mautic6_core_patch_policy", "required") != "required":
        return {"status": "skip", "reason": "policy_off", "root": install.root}
    return run(config, install, phase="before_plugin_reload", trigger=trigger)


def revert_m6_plugin_update_metadata_patch(install, config: Any = None, *, run_id=None):
    return run(config, install, phase="before_plugin_reload", operation="rollback", trigger="operator_action", run_id=run_id)
