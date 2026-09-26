"""Signed host-local backup attestation for an immutable upgrade patch plan."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from typing import Any

from mcd_agent.mautic_json_repair import (
    RepairAuthorizationError, _canonical, _read_signing_key,
    load_authorization_context, verify_backup_evidence,
)
from mcd_agent.mautic_patch_resolution import canonical_json_sha256

SCHEMA = "mcd-mautic-patch-backup-attestation-v1"
FIELDS = {"schema", "instance_uid", "root", "source_version", "target_version",
          "run_id", "plan_sha256", "backup_evidence", "issued_at", "expires_at", "nonce", "signature"}
BACKUP_FIELDS = {"backup_id", "backup_path", "manifest_path", "sha256", "completed_at", "bytes_written"}


def required(plan: dict[str, Any]) -> bool:
    return str(plan.get("source_version", "")).split(".")[0] == "6" and str(plan.get("target_version", "")).split(".")[0] == "7"


def _backup(value: Any, *, root: str, instance_uid: str, now: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != BACKUP_FIELDS:
        raise RepairAuthorizationError("patch_backup_fields_invalid")
    size = value.get("bytes_written")
    directory = Path(str(value.get("backup_path") or ""))
    manifest = Path(str(value.get("manifest_path") or ""))
    if (not str(value.get("backup_id", "")).startswith(instance_uid + ":")
            or not directory.is_absolute() or manifest != directory / ".mcd-backup.json"
            or directory.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", "")))
            or isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        raise RepairAuthorizationError("patch_backup_evidence_invalid")
    verify_backup_evidence(value, root=root, instance_uid=instance_uid, now=now)
    marker = json.loads(manifest.read_text(encoding="utf-8"))
    if marker.get("bytes_written") != size:
        raise RepairAuthorizationError("patch_backup_size_mismatch")
    return dict(value)


def validate(context: Any, *, plan: dict[str, Any], instance_uid: str,
             root: str, signing_key: bytes, now: int | None = None) -> dict[str, Any]:
    current = int(time.time() if now is None else now)
    if not isinstance(context, dict) or set(context) != FIELDS or context.get("schema") != SCHEMA:
        raise RepairAuthorizationError("patch_backup_attestation_invalid")
    expected = {"instance_uid": instance_uid, "root": root,
                "source_version": plan["source_version"], "target_version": plan["target_version"],
                "run_id": plan["run_id"], "plan_sha256": canonical_json_sha256(plan)}
    if not instance_uid or any(context.get(key) != value for key, value in expected.items()):
        raise RepairAuthorizationError("patch_backup_binding_mismatch")
    issued, expires = context.get("issued_at"), context.get("expires_at")
    if (type(issued) is not int or type(expires) is not int
            or not issued <= current < expires or not 0 < expires - issued <= 3600
            or not re.fullmatch(r"[0-9a-f]{32}", str(context.get("nonce", "")))):
        raise RepairAuthorizationError("patch_backup_attestation_expired")
    signature = context.get("signature")
    payload = {key: value for key, value in context.items() if key != "signature"}
    expected_signature = hmac.new(signing_key, _canonical(payload), hashlib.sha256).hexdigest()
    if len(signing_key) < 16 or not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(signature, expected_signature):
        raise RepairAuthorizationError("patch_backup_signature_invalid")
    return _backup(context["backup_evidence"], root=root, instance_uid=instance_uid, now=current)


def load_and_validate(path: str, *, key_path: str, plan: dict[str, Any],
                      instance_uid: str, root: str) -> dict[str, Any]:
    context, key = load_authorization_context(path, key_path=key_path)
    return validate(context, plan=plan, instance_uid=instance_uid, root=root, signing_key=key)


def issue(*, plan: dict[str, Any], instance_uid: str, root: str,
          backup_evidence: dict[str, Any], key_path: str,
          output_path: str) -> dict[str, Any]:
    from mcd_agent.mautic_patch_plan_v3 import _validate_plan
    _validate_plan(plan)
    current = int(time.time())
    key = _read_signing_key(key_path)
    backup = _backup(backup_evidence, root=root, instance_uid=instance_uid, now=current)
    context = {"schema": SCHEMA, "instance_uid": instance_uid, "root": root,
               "source_version": plan["source_version"], "target_version": plan["target_version"],
               "run_id": plan["run_id"], "plan_sha256": canonical_json_sha256(plan),
               "backup_evidence": backup, "issued_at": current, "expires_at": current + 3600,
               "nonce": secrets.token_hex(16)}
    context["signature"] = hmac.new(key, _canonical(context), hashlib.sha256).hexdigest()
    validate(context, plan=plan, instance_uid=instance_uid, root=root, signing_key=key, now=current)
    destination = Path(output_path)
    if not destination.is_absolute() or destination.is_symlink():
        raise RepairAuthorizationError("patch_backup_output_path_invalid")
    from mcd_agent.mautic_patch_plan_v3 import _save_ledger
    _save_ledger(destination, context)
    return {"status": "success", "schema": SCHEMA, "context_path": str(destination),
            "plan_sha256": context["plan_sha256"], "run_id": context["run_id"],
            "backup_id": backup["backup_id"], "backup_sha256": backup["sha256"],
            "expires_at": context["expires_at"]}
