"""Authenticated transport for MCC-resolved Mautic patch plans."""
from __future__ import annotations

import hashlib
import importlib.resources
import json
import base64
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


RESOLUTION_SCHEMA = "mcd-mautic-patch-resolution-v1"
REQUEST_SCHEMA = "mcc-mautic-patch-resolve-v1"
RESPONSE_SCHEMA = "mcc-mautic-patch-resolved-v1"
PLAN_SCHEMA = "mcd-mautic-patch-plan-v3"
EVIDENCE_SCHEMA = "mcd-mautic-patch-evidence-v1"
EVIDENCE_ACCEPTED_SCHEMA = "mcc-mautic-patch-evidence-accepted-v1"

_TRIGGERS = {"daemon_reconcile", "plugin_reload_failure", "upgrade_lifecycle", "operator_action"}
_OPERATIONS = {"status", "verify", "apply", "rollback"}
_PHASES = {
    "before_background_imports",
    "before_cache_warmup",
    "before_campaign_execution",
    "before_doctrine_migrations",
    "post_source_install",
    "before_plugin_reload",
    "dependency_update_preflight",
    "before_asset_generation",
    "preflight_frontend_assets",
    "legacy_runtime_plugin_repair",
    "image_build_before_packaging",
}
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class MauticPatchResolutionError(RuntimeError):
    """The MCC catalog could not safely resolve or record a patch operation."""


def _contract() -> dict[str, Any]:
    path = importlib.resources.files("mcd_agent").joinpath(
        "contracts/mautic-patch-resolution-v1.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MauticPatchResolutionError("resolution_contract_unavailable") from exc
    if value.get("schema") != RESOLUTION_SCHEMA or value.get("minimum_agent_version") != __version__:
        raise MauticPatchResolutionError("resolution_contract_version_mismatch")
    return value


def canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _endpoint(config: AgentConfig, suffix: str) -> str:
    base = str(getattr(config, "mcc_url", "") or "").strip().rstrip("/")
    if not base:
        raise MauticPatchResolutionError("mcc_url_missing")
    if base.endswith("/api/v1"):
        return base + suffix
    return base + "/api/v1" + suffix


def _post_json(config: AgentConfig, suffix: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = str(getattr(config, "mcc_token", "") or "").strip()
    if not token:
        raise MauticPatchResolutionError("mcc_token_missing")
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(config, suffix),
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise MauticPatchResolutionError(f"mcc_patch_http_{int(exc.code)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MauticPatchResolutionError(f"mcc_patch_transport_failed: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MauticPatchResolutionError("mcc_patch_response_invalid_json") from exc
    if not isinstance(value, dict):
        raise MauticPatchResolutionError("mcc_patch_response_not_object")
    return value


def _host_identity(config: AgentConfig) -> tuple[str, str]:
    identity = resolve_agent_identity(config)
    host_name = str(identity.get("effective_mcc_host_name") or "").strip()
    hostname = str(identity.get("effective_hostname") or "").strip()
    if not host_name or not hostname:
        raise MauticPatchResolutionError("mcc_host_identity_unavailable")
    return host_name, hostname


def _resolved_host_id(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise MauticPatchResolutionError("mcc_resolved_host_id_invalid") from exc
    if str(parsed) != raw.lower():
        raise MauticPatchResolutionError("mcc_resolved_host_id_invalid")
    return raw


def _agent_capabilities(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        key: contract[key]
        for key in (
            "schema",
            "minimum_agent_version",
            "plan_schema",
            "evidence_schema",
            "triggers",
            "operations",
            "execution_kinds",
            "phases",
            "gate_kinds",
            "gate_groups",
            "max_plan_bytes",
            "max_payload_bytes",
            "install_types",
        )
    }


def resolve_plan(
    config: AgentConfig,
    install: Any,
    *,
    trigger: str,
    phase: str,
    operation: str,
    observed_version: str | None,
    install_type: str,
    observed_major: int | None = None,
    target_version: str | None = None,
    run_id: str | None = None,
    cached_catalog_revision: str | None = None,
    cached_catalog_sha256: str | None = None,
) -> dict[str, Any]:
    contract = _contract()
    if trigger not in _TRIGGERS or trigger not in contract["triggers"]:
        raise MauticPatchResolutionError("unsupported_patch_trigger")
    if phase not in _PHASES or phase not in contract["phases"]:
        raise MauticPatchResolutionError("unsupported_patch_phase")
    if operation not in _OPERATIONS or operation not in contract["operations"]:
        raise MauticPatchResolutionError("unsupported_patch_operation")
    if operation == "rollback" and not run_id:
        raise MauticPatchResolutionError("rollback_run_id_required")
    if run_id is None and operation != "rollback":
        run_id = uuid.uuid4().hex
    if run_id is not None and not _RUN_ID_RE.fullmatch(str(run_id)):
        raise MauticPatchResolutionError("invalid_patch_run_id")
    if observed_version is None and (isinstance(observed_major, bool) or observed_major not in range(1, 100)):
        raise MauticPatchResolutionError("unknown_version_requires_confirmed_major")

    host_name, hostname = _host_identity(config)
    instance_uid = str(getattr(install, "instance_uid", "") or "").strip()
    install_type = str(install_type or "").strip().lower()
    if not instance_uid or install_type not in set(contract["install_types"]):
        raise MauticPatchResolutionError("patch_instance_identity_invalid")

    request_payload = {
        "schema": REQUEST_SCHEMA,
        "mcc_host_name": host_name,
        "hostname": hostname,
        "agent_version": __version__,
        "instance_uid": instance_uid,
        "observed_version": observed_version,
        "observed_major": observed_major,
        "target_version": target_version,
        "install_type": install_type,
        "trigger": trigger,
        "phase": phase,
        "operation": operation,
        "run_id": run_id,
        "cached_catalog_revision": cached_catalog_revision,
        "cached_catalog_sha256": cached_catalog_sha256,
        "agent_patch_contract": _agent_capabilities(contract),
    }
    result = _post_json(config, "/agent/mautic-patches/resolve", request_payload)
    if result.get("schema") != RESPONSE_SCHEMA:
        raise MauticPatchResolutionError("mcc_patch_response_schema_mismatch")
    if result.get("instance_uid") != instance_uid:
        raise MauticPatchResolutionError("mcc_patch_instance_mismatch")
    _resolved_host_id(result.get("host_id"))
    if result.get("status") not in {"selected", "noop", "blocked"}:
        raise MauticPatchResolutionError("mcc_patch_response_status_invalid")
    if result["status"] != "selected":
        if result.get("plan") is not None:
            raise MauticPatchResolutionError("mcc_nonselected_response_has_plan")
        return result

    plan = result.get("plan")
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise MauticPatchResolutionError("mcc_patch_plan_schema_mismatch")
    if result.get("plan_sha256") != canonical_json_sha256(plan):
        raise MauticPatchResolutionError("mcc_patch_plan_sha256_mismatch")
    if not _SHA40_RE.fullmatch(str(result.get("registry_commit", ""))):
        raise MauticPatchResolutionError("mcc_registry_commit_invalid")
    if not _SHA64_RE.fullmatch(str(result.get("registry_sha256", ""))):
        raise MauticPatchResolutionError("mcc_registry_sha256_invalid")
    if plan.get("registry_commit") != result.get("registry_commit") or plan.get("registry_sha256") != result.get("registry_sha256"):
        raise MauticPatchResolutionError("mcc_plan_registry_mismatch")
    if plan.get("instance_uid") not in (None, instance_uid):
        raise MauticPatchResolutionError("mcc_plan_instance_mismatch")
    if plan.get("trigger") != trigger or plan.get("phase") != phase or plan.get("operation") != operation:
        raise MauticPatchResolutionError("mcc_plan_context_mismatch")
    if plan.get("run_id") != run_id:
        raise MauticPatchResolutionError("mcc_plan_run_id_mismatch")
    source = str(plan.get("source_version", "") or "")
    target = str(plan.get("target_version", "") or "")
    source_tuple = _version_tuple(source)
    target_tuple = _version_tuple(target)
    if trigger == "upgrade_lifecycle":
        if source_tuple is None or target_tuple is None or source_tuple >= target_tuple:
            raise MauticPatchResolutionError("upgrade_plan_version_relation_invalid")
    elif source or target:
        if source_tuple is None or target_tuple is None or source_tuple > target_tuple:
            raise MauticPatchResolutionError("remediation_plan_version_relation_invalid")
    if len(json.dumps(plan, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > int(contract["max_plan_bytes"]):
        raise MauticPatchResolutionError("mcc_patch_plan_too_large")
    payloads = plan.get("payloads", [])
    if not isinstance(payloads, list):
        raise MauticPatchResolutionError("mcc_patch_payloads_invalid")
    payload_bytes = 0
    for item in payloads:
        if not isinstance(item, dict) or not isinstance(item.get("content_base64"), str):
            raise MauticPatchResolutionError("mcc_patch_payload_invalid")
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise MauticPatchResolutionError("mcc_patch_payload_invalid_base64") from exc
        payload_bytes += len(content)
        if not _SHA64_RE.fullmatch(str(item.get("sha256", ""))):
            raise MauticPatchResolutionError("mcc_patch_payload_sha256_invalid")
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise MauticPatchResolutionError("mcc_patch_payload_sha256_mismatch")
    if payload_bytes > int(contract["max_payload_bytes"]):
        raise MauticPatchResolutionError("mcc_patch_payload_too_large")
    return result


def report_evidence(
    config: AgentConfig,
    *,
    instance_uid: str,
    mcc_host_name: str,
    hostname: str,
    host_id: str,
    catalog_revision: str,
    catalog_sha256: str,
    plan_sha256: str,
    trigger: str,
    phase: str,
    run_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    contract = _contract()
    if trigger not in _TRIGGERS or phase not in _PHASES or not _RUN_ID_RE.fullmatch(run_id):
        raise MauticPatchResolutionError("patch_evidence_context_invalid")
    if not _SHA64_RE.fullmatch(catalog_sha256) or not _SHA64_RE.fullmatch(plan_sha256):
        raise MauticPatchResolutionError("patch_evidence_hash_invalid")
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "mcc_host_name": mcc_host_name,
        "hostname": hostname,
        "host_id": _resolved_host_id(host_id),
        "instance_uid": instance_uid,
        "catalog_revision": catalog_revision,
        "catalog_sha256": catalog_sha256,
        "plan_sha256": plan_sha256,
        "trigger": trigger,
        "phase": phase,
        "run_id": run_id,
        "evidence": evidence,
    }
    result = _post_json(config, "/agent/mautic-patches/evidence", payload)
    if result.get("schema") != EVIDENCE_ACCEPTED_SCHEMA or result.get("status") != "accepted":
        raise MauticPatchResolutionError("mcc_patch_evidence_not_accepted")
    if not str(result.get("evidence_id", "") or "").strip():
        raise MauticPatchResolutionError("mcc_patch_evidence_id_missing")
    return result
