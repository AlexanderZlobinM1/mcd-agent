"""Bounded read-only source-signature collector; no exclusion authority."""
import json
import re

from mcd_agent.mautic_patch_plan_v3 import _root, _record_state
from mcd_agent.mautic_patch_resolution import _validate_gates, canonical_json_sha256


def collect_fixed_signature(root, *, record, expected_record_sha256):
    if not isinstance(record, dict) or canonical_json_sha256(record) != expected_record_sha256:
        raise ValueError("check_only_record_binding_mismatch")
    if len(json.dumps(record, ensure_ascii=True, allow_nan=False).encode()) > 262144:
        raise ValueError("check_only_record_too_large")
    if not isinstance(record.get("id"), str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,95}", record["id"]):
        raise ValueError("check_only_record_id_invalid")
    paths = record.get("source_paths")
    if (not isinstance(paths, list) or not paths or len(paths) > 512
            or any(type(p) is not str or len(p.encode()) > 512 for p in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError("check_only_source_paths_invalid")
    if record.get("gate_logic") not in {
        "vulnerable_all; fixed_all; mixed_or_unknown=error",
        "vulnerable_exactly_one; fixed_exactly_one; mixed_or_unknown=error",
    }:
        raise ValueError("check_only_gate_logic_invalid")
    gates = record.get("gate")
    if not isinstance(gates, list) or len(gates) > 512:
        raise ValueError("check_only_gate_count_invalid")
    _validate_gates(record, set(paths))
    state, observations = _record_state(_root(str(root)), record)
    if any(row.get("actual") is None if row["kind"] == "exact_count"
           else row.get("actual_sha256") is None if row["kind"] == "sha256"
           else False for row in observations):
        raise ValueError("check_only_unknown_signature_observation")
    if state != "already":
        raise ValueError("check_only_complete_fixed_signature_unproven")
    return dict(schema="mcd-mautic-fixed-signature-observation-v1", patch_id=record["id"],
        record_sha256=expected_record_sha256, decision="already", gates=observations)
