"""Registry-bound generic Mautic patch plans (v2).

The MCC transport is trusted to attest that the unchanged catalog records came
from the immutable Operations commit. This module independently checks record
shape, applicability, source signatures, payload bytes and phase safety before
applying anything on the host.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from importlib.resources import files
from typing import Any

SCHEMA = "mcd-mautic-patch-plan-v2"
EVIDENCE_SCHEMA = "mcd-mautic-patch-preflight-v2"
MAX_PLAN_BYTES = 1_048_576
MAX_PAYLOAD_BYTES = 131_072
PHASES = frozenset({
    "post_source_install",
    "before_doctrine_migrations",
    "before_background_imports",
    "before_campaign_execution",
    "before_cache_warmup",
})
INSTALL_TYPES = frozenset({"composer", "zip"})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_RANGE_TOKEN = re.compile(r"^(>=|<=|>|<|=)?(\d+\.\d+\.\d+)$")
_DIFF_PATH = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)


class PatchPlanV2Error(RuntimeError):
    pass


def capability() -> dict[str, Any]:
    contract_path = files("mcd_agent").joinpath("contracts/mautic-patch-plan-v2.json")
    return json.loads(contract_path.read_text(encoding="utf-8"))


def _version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise PatchPlanV2Error("invalid_semantic_version")
    match = _VERSION.fullmatch(value)
    if not match:
        raise PatchPlanV2Error("invalid_semantic_version")
    return tuple(int(part) for part in match.groups())


def _range_matches(expression: str, target: tuple[int, int, int]) -> bool:
    # Registry text after "while" describes its signature condition; ranges
    # before it are the machine-readable version expression.
    value = expression.split(" while ", 1)[0].strip()
    if re.fullmatch(r"\d+\.\d+\.x", value):
        major, minor = (int(v) for v in value[:-2].split("."))
        return target[:2] == (major, minor)
    tokens = value.split()
    if not tokens:
        return False
    for token in tokens:
        match = _RANGE_TOKEN.fullmatch(token)
        if not match:
            return False
        op = match.group(1) or "="
        bound = _version(match.group(2))
        if op == ">=" and not target >= bound:
            return False
        if op == "<=" and not target <= bound:
            return False
        if op == ">" and not target > bound:
            return False
        if op == "<" and not target < bound:
            return False
        if op == "=" and target != bound:
            return False
    return True


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PatchPlanV2Error("unsafe_relative_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PatchPlanV2Error("unsafe_relative_path")
    return path.as_posix()


def _validate_record(record: Any, target: tuple[int, int, int], install_type: str) -> dict[str, Any]:
    required = {
        "id", "status", "owner", "enabled", "execution_scope", "affected_ranges",
        "fixed_ranges", "install_types", "phases", "depends_on", "conflicts_with",
        "phase_order", "gate_logic", "patch_path", "patch_sha256", "source_paths",
        "gate",
    }
    if not isinstance(record, dict) or not required.issubset(record):
        raise PatchPlanV2Error("incomplete_catalog_record")
    if (record["status"] != "active" or record["enabled"] is not True
            or record["execution_scope"] != "generic_upgrade"):
        raise PatchPlanV2Error("record_not_generic_active")
    if not isinstance(record["id"], str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,95}", record["id"]):
        raise PatchPlanV2Error("invalid_patch_id")
    if install_type not in record["install_types"]:
        raise PatchPlanV2Error("record_install_type_unsupported")
    ranges = record["affected_ranges"]
    if not isinstance(ranges, list) or not ranges or not any(
        isinstance(item, str) and _range_matches(item, target) for item in ranges
    ):
        raise PatchPlanV2Error("target_outside_affected_ranges")
    fixed_ranges = record["fixed_ranges"]
    if not isinstance(fixed_ranges, list) or any(not isinstance(item, str) for item in fixed_ranges):
        raise PatchPlanV2Error("invalid_fixed_ranges")
    if not isinstance(record["phases"], list) or not record["phases"]:
        raise PatchPlanV2Error("invalid_record_phases")
    if any(phase not in PHASES for phase in record["phases"]):
        raise PatchPlanV2Error("unsupported_record_phase")
    if isinstance(record["phase_order"], bool) or not isinstance(record["phase_order"], int) or record["phase_order"] < 0:
        raise PatchPlanV2Error("invalid_phase_order")
    if record["gate_logic"] not in {
        "vulnerable_all; fixed_all; mixed_or_unknown=error",
        "vulnerable_exactly_one; fixed_exactly_one; mixed_or_unknown=error",
    }:
        raise PatchPlanV2Error("unsupported_gate_logic")
    if not isinstance(record["depends_on"], list) or not isinstance(record["conflicts_with"], list):
        raise PatchPlanV2Error("invalid_patch_dependencies")
    if not isinstance(record["source_paths"], list) or not record["source_paths"]:
        raise PatchPlanV2Error("invalid_source_paths")
    record["source_paths"] = [_safe_relative(path) for path in record["source_paths"]]
    if not isinstance(record["patch_path"], str) or not record["patch_path"]:
        raise PatchPlanV2Error("missing_patch_payload_path")
    record["patch_path"] = _safe_relative(record["patch_path"])
    if not isinstance(record["patch_sha256"], str) or not _HEX64.fullmatch(record["patch_sha256"]):
        raise PatchPlanV2Error("invalid_patch_payload_sha256")
    gates = record["gate"]
    if not isinstance(gates, list) or not gates:
        raise PatchPlanV2Error("missing_signature_gates")
    groups: set[str] = set()
    for gate in gates:
        if not isinstance(gate, dict) or not {"group", "kind", "path", "needle", "expected_count"}.issubset(gate) or set(gate) - {
            "group", "kind", "path", "needle", "expected_count", "allow_missing_path"
        }:
            raise PatchPlanV2Error("unsupported_gate_shape")
        if gate["kind"] != "exact_count" or gate["group"] not in {"vulnerable", "fixed"}:
            raise PatchPlanV2Error("unsupported_gate_kind_or_group")
        if gate["group"] == "vulnerable":
            groups.add("vulnerable")
        if not isinstance(gate["needle"], str) or not gate["needle"]:
            raise PatchPlanV2Error("invalid_literal_gate_needle")
        if isinstance(gate["expected_count"], bool) or not isinstance(gate["expected_count"], int) or gate["expected_count"] < 0:
            raise PatchPlanV2Error("invalid_gate_expected_count")
        if "allow_missing_path" in gate and not isinstance(gate["allow_missing_path"], bool):
            raise PatchPlanV2Error("invalid_allow_missing_path")
        if gate.get("allow_missing_path") is True and gate["expected_count"] != 0:
            raise PatchPlanV2Error("allow_missing_requires_zero_count")
        if gate["group"] == "fixed" and gate["expected_count"] == 0:
            raise PatchPlanV2Error("fixed_gate_must_be_positive")
        gate["path"] = _safe_relative(gate["path"])
    if "vulnerable" not in groups:
        raise PatchPlanV2Error("missing_vulnerable_gate_group")
    return record


def parse_plan(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_PLAN_BYTES:
        raise PatchPlanV2Error("plan_too_large")
    try:
        plan = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PatchPlanV2Error("invalid_plan_json") from exc
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise PatchPlanV2Error("unknown_plan_schema")
    required = {"schema", "registry_commit", "registry_sha256", "source_version", "target_version",
                "install_type", "phase", "run_id", "operation", "patches", "payloads"}
    if set(plan) != required:
        raise PatchPlanV2Error("unknown_or_missing_plan_fields")
    if not isinstance(plan["registry_commit"], str) or not _HEX40.fullmatch(plan["registry_commit"]):
        raise PatchPlanV2Error("invalid_registry_commit")
    if not isinstance(plan["registry_sha256"], str) or not _HEX64.fullmatch(plan["registry_sha256"]):
        raise PatchPlanV2Error("invalid_registry_sha256")
    source = _version(plan["source_version"])
    target = _version(plan["target_version"])
    if source[0] != target[0] or target <= source:
        raise PatchPlanV2Error("unsupported_transition")
    if plan["install_type"] not in INSTALL_TYPES:
        raise PatchPlanV2Error("unsupported_install_type")
    if plan["phase"] not in PHASES:
        raise PatchPlanV2Error("unsupported_phase")
    if not isinstance(plan["run_id"], str) or not _RUN_ID.fullmatch(plan["run_id"]):
        raise PatchPlanV2Error("invalid_run_id")
    if plan["operation"] not in {"apply", "verify"}:
        raise PatchPlanV2Error("unsupported_operation")
    if not isinstance(plan["patches"], list) or not plan["patches"] or len(plan["patches"]) > 32:
        raise PatchPlanV2Error("invalid_patch_records")
    seen: set[str] = set()
    checked_records = []
    for original in plan["patches"]:
        record = _validate_record(dict(original) if isinstance(original, dict) else original, target, plan["install_type"])
        if record["id"] in seen:
            raise PatchPlanV2Error("duplicate_patch_id")
        seen.add(record["id"])
        checked_records.append(record)
    for record in checked_records:
        if any(dep not in seen for dep in record["depends_on"]):
            raise PatchPlanV2Error("missing_patch_dependency")
        if any(conflict in seen for conflict in record["conflicts_with"]):
            raise PatchPlanV2Error("patch_conflict")
    if not isinstance(plan["payloads"], list) or len(plan["payloads"]) != len(checked_records):
        raise PatchPlanV2Error("payload_record_count_mismatch")
    payload_map: dict[str, bytes] = {}
    for payload in plan["payloads"]:
        if not isinstance(payload, dict) or set(payload) != {"path", "sha256", "content_base64"}:
            raise PatchPlanV2Error("invalid_payload_shape")
        path = _safe_relative(payload["path"])
        if path in payload_map or not isinstance(payload["sha256"], str) or not _HEX64.fullmatch(payload["sha256"]):
            raise PatchPlanV2Error("duplicate_or_invalid_payload")
        try:
            data = base64.b64decode(payload["content_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise PatchPlanV2Error("invalid_payload_base64") from exc
        if not data or len(data) > MAX_PAYLOAD_BYTES or hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise PatchPlanV2Error("payload_sha256_mismatch_or_size")
        payload_map[path] = data
    for record in checked_records:
        data = payload_map.get(record["patch_path"])
        if data is None or hashlib.sha256(data).hexdigest() != record["patch_sha256"]:
            raise PatchPlanV2Error("record_payload_mismatch")
    plan["patches"] = sorted(checked_records, key=lambda item: (item["phase_order"], item["id"]))
    plan["_payload_map"] = payload_map
    return plan


def _safe_file(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    resolved = path.resolve(strict=False)
    if root not in resolved.parents or path.is_symlink():
        raise PatchPlanV2Error("source_path_escape")
    return path


def _patch_paths(data: bytes) -> set[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchPlanV2Error("non_text_patch_unsupported") from exc
    if "GIT binary patch" in text or "\nrename from " in text or "\nnew file mode " in text or "\ndeleted file mode " in text:
        raise PatchPlanV2Error("unsupported_patch_operation")
    pairs = _DIFF_PATH.findall(text)
    if not pairs:
        raise PatchPlanV2Error("invalid_unified_patch")
    paths = set()
    for old, new in pairs:
        old_path = _safe_relative(old)
        new_path = _safe_relative(new)
        if old_path != new_path:
            raise PatchPlanV2Error("patch_rename_unsupported")
        paths.add(old_path)
    return paths


def _gate_results(root: Path, record: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    counts: dict[str, list[bool]] = {"vulnerable": [], "fixed": []}
    evidence = []
    for gate in record["gate"]:
        path = _safe_file(root, gate["path"])
        if not path.is_file():
            if gate.get("allow_missing_path") is not True or gate["expected_count"] != 0:
                raise PatchPlanV2Error("source_gate_path_missing")
            actual = 0
        else:
            try:
                contents = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise PatchPlanV2Error("source_gate_read_failed") from exc
            actual = contents.count(gate["needle"])
        expected = gate["expected_count"]
        counts[gate["group"]].append(actual == expected)
        evidence.append({"group": gate["group"], "path": gate["path"],
                         "needle_sha256": hashlib.sha256(gate["needle"].encode()).hexdigest(),
                         "expected": expected, "actual": actual,
                         "allow_missing_path": gate.get("allow_missing_path", False)})
    vuln_ok = bool(counts["vulnerable"]) and all(counts["vulnerable"])
    fixed_evidence = [item for item in evidence if item["group"] == "fixed"]
    fixed_ok = bool(fixed_evidence) and all(counts["fixed"])
    fixed_zero = all(item["actual"] == 0 for item in fixed_evidence)
    vulnerable_positive_present = any(
        item["expected"] > 0 and item["actual"] > 0
        for item in evidence if item["group"] == "vulnerable"
    )
    # Fixed signatures take precedence over vulnerable absence-gates, but
    # simultaneous positive vulnerable/fixed signatures are a mixed tree.
    if fixed_ok:
        if not vulnerable_positive_present:
            return "fixed", evidence
        return "ambiguous_or_unknown", evidence
    if vuln_ok and (not fixed_evidence or fixed_zero):
        return "vulnerable", evidence
    return "ambiguous_or_unknown", evidence


def _canonical_root(root_value: str, install_type: str) -> tuple[Path, Path]:
    instance = Path(root_value).resolve(strict=True)
    if not instance.is_dir() or instance == Path("/"):
        raise PatchPlanV2Error("invalid_project_root")
    source = instance / "docroot" if install_type == "composer" and (instance / "docroot").is_dir() else instance
    source = source.resolve(strict=True)
    from mcd_agent.install_type import detect_install_type
    if detect_install_type(str(instance)) != install_type:
        raise PatchPlanV2Error("install_type_mismatch")
    if source != instance and instance not in source.parents:
        raise PatchPlanV2Error("source_root_escape")
    return instance, source


def _plan_sha(plan: dict[str, Any]) -> str:
    public = {key: value for key, value in plan.items() if not key.startswith("_")}
    return hashlib.sha256(json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ledger_path(instance: Path, run_id: str, phase: str) -> Path:
    directory = instance
    for part in (".mcd", "patch-runs", "v2", run_id):
        directory = directory / part
        if directory.exists() and directory.is_symlink():
            raise PatchPlanV2Error("unsafe_patch_ledger_path")
        directory.mkdir(exist_ok=True, mode=0o700)
        if instance not in directory.resolve(strict=True).parents:
            raise PatchPlanV2Error("unsafe_patch_ledger_path")
    return directory / (phase + ".json")


def _restore_snapshots(source: Path, snapshots: list[dict[str, Any]], expected_after: dict[str, str]) -> bool:
    ok = True
    for snapshot in reversed(snapshots):
        relative = snapshot["path"]
        path = _safe_file(source, relative)
        if not path.is_file() or _sha_file(path) != expected_after.get(relative):
            ok = False
            continue
        path.write_bytes(base64.b64decode(snapshot["content_base64"], validate=True))
        path.chmod(snapshot["mode"])
        if os.geteuid() == 0:
            os.chown(path, snapshot["uid"], snapshot["gid"])
    return ok


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(root_value: str, raw_plan: str) -> dict[str, Any]:
    plan = parse_plan(raw_plan)
    instance, source = _canonical_root(root_value, plan["install_type"])
    from mcd_agent.mautic_patch_plan import _target_version
    installed_target = _target_version(instance)
    if installed_target != plan["target_version"]:
        raise PatchPlanV2Error("target_version_mismatch")
    plan_hash = _plan_sha(plan)
    payloads = plan["_payload_map"]
    records = [record for record in plan["patches"] if plan["phase"] in record["phases"]]
    decisions = []
    to_apply = []
    for record in records:
        state, gates = _gate_results(source, record)
        if state == "ambiguous_or_unknown":
            decisions.append({"id": record["id"], "decision": "error", "gates": gates,
                              "reason": "ambiguous_or_unknown_gate"})
            continue
        if state == "fixed":
            decisions.append({"id": record["id"], "decision": "already", "gates": gates,
                              "payload_sha256": record["patch_sha256"]})
            continue
        target_parts = _version(plan["target_version"])
        if any(_range_matches(item, target_parts) for item in record["fixed_ranges"]):
            decisions.append({"id": record["id"], "decision": "error", "gates": gates,
                              "reason": "fixed_range_without_fixed_signature"})
            continue
        if plan["operation"] == "verify":
            decisions.append({"id": record["id"], "decision": "candidate", "gates": gates,
                              "payload_sha256": record["patch_sha256"]})
            continue
        paths = _patch_paths(payloads[record["patch_path"]])
        if not paths.issubset(set(record["source_paths"])):
            decisions.append({"id": record["id"], "decision": "error", "gates": gates,
                              "reason": "patch_paths_not_declared"})
            continue
        patch_bytes = payloads[record["patch_path"]]
        check = subprocess.run(["git", "apply", "--check", "--"], cwd=source,
                               input=patch_bytes, capture_output=True, timeout=30)
        if check.returncode != 0:
            decisions.append({"id": record["id"], "decision": "error", "gates": gates,
                              "reason": "patch_apply_check_failed"})
            continue
        before = {}
        for relative in sorted(paths):
            path = _safe_file(source, relative)
            if not path.is_file():
                raise PatchPlanV2Error("patch_target_missing")
            data = path.read_bytes()
            metadata = path.stat()
            before[relative] = {"bytes": data, "mode": stat.S_IMODE(metadata.st_mode),
                                "uid": metadata.st_uid, "gid": metadata.st_gid,
                                "sha256": hashlib.sha256(data).hexdigest()}
        to_apply.append((record, gates, patch_bytes, paths, before))
    if any(item["decision"] == "error" for item in decisions):
        status = "error"
        to_apply = []
    elif plan["operation"] == "verify":
        status = "success"
    else:
        status = "success"
    applied = []
    rollback_succeeded = True
    rollback_attempted = False
    if status == "success" and plan["operation"] == "apply":
        for record, gates, patch_bytes, paths, before in to_apply:
            result = subprocess.run(["git", "apply", "--"], cwd=source, input=patch_bytes,
                                    capture_output=True, timeout=30)
            if result.returncode != 0:
                status = "error"
                decisions.append({"id": record["id"], "decision": "error", "gates": gates,
                                  "reason": "patch_apply_failed"})
                break
            after = {rel: _sha_file(_safe_file(source, rel)) for rel in paths}
            current, after_gates = _gate_results(source, record)
            if current != "fixed":
                status = "error"
                decisions.append({"id": record["id"], "decision": "error", "gates": after_gates,
                                  "reason": "post_apply_fixed_gate_failed"})
                serialized = [{"path": rel, "content_base64": base64.b64encode(snap["bytes"]).decode(),
                               "mode": snap["mode"], "uid": snap["uid"], "gid": snap["gid"]}
                              for rel, snap in before.items()]
                rollback_attempted = True
                rollback_succeeded = _restore_snapshots(source, serialized, after)
                break
            applied.append({"record": record, "before": before, "after": after})
            decisions.append({"id": record["id"], "decision": "applied", "gates": after_gates,
                              "payload_sha256": record["patch_sha256"], "before_sha256": {k: v["sha256"] for k, v in before.items()},
                              "after_sha256": after})
        if status == "error":
            for applied_item in reversed(applied):
                serialized = [{"path": rel, "content_base64": base64.b64encode(snap["bytes"]).decode(),
                               "mode": snap["mode"], "uid": snap["uid"], "gid": snap["gid"]}
                              for rel, snap in applied_item["before"].items()]
                rollback_attempted = True
                rollback_succeeded = _restore_snapshots(source, serialized, applied_item["after"]) and rollback_succeeded
            applied.clear()
    if status == "success" and plan["operation"] == "apply" and applied:
        ledger = _ledger_path(instance, plan["run_id"], plan["phase"])
        ledger_data = {"schema": EVIDENCE_SCHEMA, "plan_sha256": plan_hash,
                       "registry_commit": plan["registry_commit"], "registry_sha256": plan["registry_sha256"],
                       "target_version": plan["target_version"], "phase": plan["phase"], "run_id": plan["run_id"],
                       "applied": [{"id": item["record"]["id"],
                                    "before": [{"path": rel, "content_base64": base64.b64encode(snap["bytes"]).decode(),
                                                "mode": snap["mode"], "uid": snap["uid"], "gid": snap["gid"]}
                                               for rel, snap in item["before"].items()],
                                    "after_sha256": item["after"]} for item in applied]}
        temporary = ledger.with_suffix(".tmp")
        temporary.write_text(json.dumps(ledger_data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(ledger)
    for decision in decisions:
        decision["phase"] = plan["phase"]
    return {"schema": EVIDENCE_SCHEMA, "status": status, "registry_commit": plan["registry_commit"],
            "registry_sha256": plan["registry_sha256"], "source_version": plan["source_version"],
            "target_version": plan["target_version"], "install_type": plan["install_type"],
            "phase": plan["phase"], "run_id": plan["run_id"], "plan_sha256": plan_hash,
            "selected": [record["id"] for record in records], "patches": decisions,
            "rollback_attempted": rollback_attempted,
            "rollback_succeeded": rollback_succeeded,
            "upgrade_started": False}


def atomic_preflight(root_value: str, raw_plan: str) -> dict[str, Any]:
    plan = parse_plan(raw_plan)
    if plan["phase"] != "post_source_install" or plan["operation"] != "apply":
        raise PatchPlanV2Error("atomic_preflight_requires_post_source_apply")
    raw_object = json.loads(raw_plan)
    phase_results = []
    phase_order = ("post_source_install", "before_doctrine_migrations")
    for phase in phase_order:
        phase_object = dict(raw_object)
        phase_object["phase"] = phase
        phase_raw = json.dumps(phase_object, ensure_ascii=True, separators=(",", ":"))
        result = execute(root_value, phase_raw)
        result["phase"] = phase
        for row in result.get("patches", []):
            row["phase"] = phase
        phase_results.append(result)
        if result["status"] != "success":
            rollback_attempted = bool(result.get("rollback_attempted", False))
            rollback_succeeded = bool(result.get("rollback_succeeded", True))
            rollback_result = {"status": "success"}
            for prior in reversed(phase_results[:-1]):
                if any(item.get("decision") == "applied" for item in prior.get("patches", [])):
                    prior_object = dict(raw_object)
                    prior_object["phase"] = prior["phase"]
                    prior_raw = json.dumps(prior_object, ensure_ascii=True, separators=(",", ":"))
                    rollback_attempted = True
                    rollback_result = rollback(root_value, prior_raw)
            rollback_succeeded = rollback_succeeded and rollback_result.get("status") == "success"
            combined_patches = [item for stage in phase_results for item in stage.get("patches", [])]
            return {"schema": EVIDENCE_SCHEMA, "status": "error", "registry_commit": plan["registry_commit"],
                    "registry_sha256": plan["registry_sha256"], "source_version": plan["source_version"],
                    "target_version": plan["target_version"], "install_type": plan["install_type"],
                    "phase": "post_source_install", "run_id": plan["run_id"],
                    "plan_sha256": _plan_sha(plan), "selected": [item["id"] for item in plan["patches"]],
                    "patches": combined_patches, "phases": phase_results,
                    "rollback_attempted": rollback_attempted,
                    "rollback_succeeded": rollback_succeeded,
                    "upgrade_started": False}
    combined_patches = [item for stage in phase_results for item in stage.get("patches", [])]
    selected = []
    for record in plan["patches"]:
        if record["id"] not in selected:
            selected.append(record["id"])
    return {"schema": EVIDENCE_SCHEMA, "status": "success", "registry_commit": plan["registry_commit"],
            "registry_sha256": plan["registry_sha256"], "source_version": plan["source_version"],
            "target_version": plan["target_version"], "install_type": plan["install_type"],
            "phase": "post_source_install", "run_id": plan["run_id"],
            "plan_sha256": _plan_sha(plan), "selected": selected, "patches": combined_patches,
            "phases": phase_results, "rollback_attempted": False, "rollback_succeeded": True,
            "upgrade_started": False}


def rollback(root_value: str, raw_plan: str) -> dict[str, Any]:
    plan = parse_plan(raw_plan)
    instance, source = _canonical_root(root_value, plan["install_type"])
    ledger = _ledger_path(instance, plan["run_id"], plan["phase"])
    if not ledger.is_file():
        raise PatchPlanV2Error("patch_rollback_ledger_missing")
    try:
        saved = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PatchPlanV2Error("patch_rollback_ledger_invalid") from exc
    if (saved.get("schema") != EVIDENCE_SCHEMA or saved.get("plan_sha256") != _plan_sha(plan)
            or saved.get("registry_commit") != plan["registry_commit"]
            or saved.get("registry_sha256") != plan["registry_sha256"]):
        raise PatchPlanV2Error("patch_rollback_plan_mismatch")
    restored = []
    for item in reversed(saved.get("applied", [])):
        if not isinstance(item, dict) or not isinstance(item.get("after_sha256"), dict):
            raise PatchPlanV2Error("patch_rollback_ledger_invalid")
        if not _restore_snapshots(source, item.get("before", []), item["after_sha256"]):
            return {"schema": EVIDENCE_SCHEMA, "status": "error", "reason": "partial_application",
                    "rollback_attempted": True, "rollback_succeeded": False, "restored": restored}
        restored.append(item.get("id"))
    return {"schema": EVIDENCE_SCHEMA, "status": "success", "run_id": plan["run_id"],
            "phase": plan["phase"], "rollback_attempted": True,
            "rollback_succeeded": True, "restored": restored}
