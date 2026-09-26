"""Compatibility names dispatch only catalog-managed import lifecycle plans."""
from __future__ import annotations
from typing import Any
from mcd_agent.mautic_patch_runtime import run, reconcile
from mcd_agent.mautic6_core_patch import detect_mautic_version


def patch_status(install, config: Any = None):
    return run(config, install, phase="before_background_imports", operation="status", trigger="operator_action")


def ensure_patch(install, config: Any = None, *, trigger="daemon_reconcile"):
    return run(config, install, phase="before_background_imports", trigger=trigger)


def revert_patch(install, config: Any = None, *, allow_other_version=False, run_id=None):
    return run(config, install, phase="before_background_imports", operation="rollback", trigger="operator_action", run_id=run_id)


def reconcile_import_tag_patch(config: Any, installs):
    return reconcile(config, installs, phase="before_background_imports")
