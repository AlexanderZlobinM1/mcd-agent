"""Catalog-only runtime and operator lifecycle entry points."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcd_agent.install_type import detect_install_type
from mcd_agent.mautic_patch_resolution import resolve_plan, report_evidence, _host_identity
from mcd_agent.mautic_patch_plan_v3 import execute
from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only, confirmed_mautic_major


def run(config: Any, install: Any, *, phase: str, operation: str = "apply",
        trigger: str = "daemon_reconcile", run_id: str | None = None) -> dict[str, Any]:
    root = str(install.root)
    if config is None or not getattr(config, "mcc_url", None):
        return {"status": "error", "root": root, "reason": "catalog_resolution_required"}
    if operation == "rollback" and not run_id:
        return {"status": "error", "root": root, "reason": "rollback_run_id_required"}
    started = datetime.now(timezone.utc).isoformat()
    try:
        if operation == "rollback":
            from mcd_agent.mautic_patch_stage import application_root
            from mcd_agent.mautic_patch_plan_v3 import _load_ledger
            ledger_path = application_root(root) / ".mcd/patch-plan-v3" / (str(run_id) + ".json")
            if ledger_path.exists():
                ledger = _load_ledger(ledger_path)
                if ledger.get("phase") != phase or ledger.get("trigger") not in {"daemon_reconcile", "operator_action", "plugin_reload_failure"}:
                    raise RuntimeError("rollback_phase_context_mismatch")
                trigger = ledger["trigger"]
        version = read_mautic_version_evidence_read_only(root).get("version")
        major = confirmed_mautic_major(root, getattr(config, "php_bin", "php"),
                                      console_path=getattr(install, "console_path", None),
                                      local_php_path=getattr(install, "local_php_path", None),
                                      run_as_user=getattr(config, "mautic_run_as_user", "www-data"))
        if major == 6 and phase in {"before_plugin_reload", "legacy_runtime_plugin_repair"} and operation == "apply":
            if getattr(config, "mautic6_core_patch_policy", "required") != "required":
                return {"status": "skip", "root": root, "reason": "policy_off"}
            if version is None and not getattr(config, "mautic6_core_patch_apply_if_version_unknown", False):
                return {"status": "skip", "root": root, "reason": "unknown_version_policy_off"}
        resolved = resolve_plan(config, install, trigger=trigger, phase=phase,
                                operation=operation, observed_version=version, observed_major=major,
                                install_type=detect_install_type(root), run_id=run_id)
        if resolved["status"] != "selected":
            return {"status": "skip" if resolved["status"] == "noop" else "error",
                    "root": root, "reason": resolved.get("reason", "catalog_not_selected")}
        plan = resolved["plan"]
        from mcd_agent.mautic_patch_stage import application_root
        result = execute(str(application_root(root)), plan, observed_major=major)
        host_name, hostname = _host_identity(config)
        evidence = {"schema": "mcd-mautic-patch-evidence-v1", "host_id": resolved["host_id"],
                    "instance_uid": install.instance_uid, "catalog_revision": resolved["catalog_revision"],
                    "catalog_sha256": resolved["catalog_sha256"], "registry_commit": plan["registry_commit"],
                    "registry_sha256": plan["registry_sha256"], "plan_sha256": resolved["plan_sha256"],
                    "trigger": trigger, "phase": phase, "run_id": plan["run_id"], "operation": operation,
                    "started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
                    "status": result["status"], "selected": result.get("selected", []),
                    "records": [{"id": row["id"], "execution_kind": next(record["execution_kind"] for record in plan["patches"] if record["id"] == row["id"]),
                                 "decision": row["decision"], "gates": row.get("gates", []),
                                 "before_sha256": row.get("before_sha256"), "after_sha256": row.get("after_sha256")}
                                for row in result.get("patches", [])],
                    "rollback": {"available": bool(result.get("rollback_available")),
                                 "attempted": bool(result.get("rollback_attempted")),
                                 "succeeded": bool(result.get("rollback_succeeded"))}}
        from mcd_agent import __version__
        evidence["agent_version"] = __version__
        report_evidence(config, instance_uid=install.instance_uid, mcc_host_name=host_name,
                        hostname=hostname, host_id=resolved["host_id"],
                        catalog_revision=resolved["catalog_revision"], catalog_sha256=resolved["catalog_sha256"],
                        plan_sha256=resolved["plan_sha256"], trigger=trigger, phase=phase,
                        run_id=plan["run_id"], evidence=evidence)
        return dict(result, root=root, evidence=evidence)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "error", "root": root, "reason": str(exc)}


def reconcile(config: Any, installs: list[Any], *, phase: str) -> list[dict[str, Any]]:
    pause = Path(getattr(config, "scheduler_pause_flag_path", "/opt/mcd/var/scheduler.pause"))
    results = []
    for install in installs:
        if pause.exists():
            break
        results.append(run(config, install, phase=phase))
    return results
