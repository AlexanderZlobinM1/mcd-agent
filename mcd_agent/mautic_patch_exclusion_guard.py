"""Read-only, fully bound conditional exclusion for ordinary upgrades."""
from __future__ import annotations

from mcd_agent.mautic_patch_facts import digest
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


def prove_not_required(plan, provider, phase):
    from mcd_agent.mautic_patch_plan_v3 import _validate_plan
    _validate_plan(plan)
    records = plan["patches"]
    if (plan["operation"] != "verify" or plan["trigger"] != "upgrade_lifecycle"
            or not records or provider is None
            or any(not row.get("preconditions") or phase not in row["phases"] for row in records)):
        raise ValueError("patch_exclusion_guard_plan_invalid")
    facts = provider(plan, "verify", phase)
    binding = facts.get("observation_binding")
    ids = [row["id"] for row in records]
    if (set(facts) != {"schema", "observation_binding", "observations_sha256", "records", "invocation", "invocation_sha256"}
            or facts["schema"] != "mcd-mautic-patch-facts-admission-v1"
            or not isinstance(binding, dict)
            or facts["observations_sha256"] != digest(binding)
            or binding.get("plan_sha256") != digest(dict(plan, operation="apply"))
            or any(binding.get(key) != plan.get(key) for key in
                ("execution_context", "run_id", "source_version", "target_version", "trigger"))
            or binding.get("plan_phase") != plan["phase"]
            or set(facts["records"]) != set(ids)
            or any(facts["records"][ident].get("preconditions") is not False for ident in ids)):
        raise ValueError("patch_exclusion_not_proven")
    invocation = {"operation": "verify", "phase": phase}
    core = {key: value for key, value in facts.items() if key not in {"invocation", "invocation_sha256"}}
    if (facts["invocation"] != invocation
            or facts["invocation_sha256"] != digest(dict(receipt=core, **invocation))):
        raise ValueError("patch_exclusion_invocation_invalid")
    return {"schema": "mcd-mautic-patch-preflight-v3", "status": "success", "operation": "verify",
            "run_id": plan["run_id"], "plan_sha256": canonical_json_sha256(plan), "selected": ids,
            "patches": [{"id": ident, "decision": "skip_condition_not_required", "gates": []} for ident in ids],
            "facts_receipt": facts, "rollback_available": False}


def read_receipt(path, expected_sha256):
    from mcd_agent.mautic_target_patch_collector import _private_bytes
    from mcd_agent.mautic_check_only_inputs import strict_json
    value = strict_json(_private_bytes(path), max_bytes=1048576)
    if not isinstance(value, dict) or canonical_json_sha256(value) != expected_sha256:
        raise ValueError("patch_exclusion_receipt_hash_invalid")
    return value


def upgrade_guard(*, config, root, current, target, install_type, run_id,
                  apply_plan_json, guard_plan_json, receipt):
    from mcd_agent.mautic_patch_fact_binding import bound_provider, require_receipt
    from mcd_agent.mautic_patch_plan import parse_plan
    guard_plan = parse_plan(guard_plan_json)
    apply_plan = parse_plan(apply_plan_json)
    if (apply_plan.get("schema") != "mcd-mautic-patch-plan-v3"
            or apply_plan.get("operation") != "apply"
            or any(guard_plan.get(key) != apply_plan.get(key) for key in
                ("run_id", "source_version", "target_version", "install_type", "trigger", "phase"))
            or guard_plan.get("run_id") != run_id or guard_plan.get("source_version") != current
            or guard_plan.get("target_version") != target or guard_plan.get("install_type") != install_type
            or ("execution_context" in apply_plan and apply_plan["execution_context"] != guard_plan.get("execution_context"))
            or {row["id"] for row in guard_plan["patches"]}.intersection(row["id"] for row in apply_plan["patches"])):
        raise ValueError("patch_exclusion_upgrade_binding_mismatch")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("invocation"), dict):
        raise ValueError("patch_exclusion_receipt_invalid")
    phase = receipt["invocation"].get("phase")
    provider = bound_provider(config, root)

    def recheck():
        fresh = prove_not_required(guard_plan, provider, phase)["facts_receipt"]
        require_receipt(receipt, fresh)
        if receipt != fresh:
            raise ValueError("patch_exclusion_full_receipt_drift")
        return fresh

    recheck()
    return recheck
