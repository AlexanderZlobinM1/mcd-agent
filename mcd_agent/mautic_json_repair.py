"""Authorized, allowlisted executor for the Mautic 6 to 7 JSON repair plan."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any

from mcd_agent.localphp import parse_local_php
from mcd_agent.models import DBConfig
from mcd_agent.mautic_upgrade_contract import (
    JSON_REPAIR_PLAN_CONTRACT,
    KNOWN_JSON_COLUMNS,
    RepairPlanError,
    _local_php_path,
    _safe_table_prefix,
    validate_json_repair_plan,
)


AUTHORIZATION_CONTRACT = "mcd-mcc-authorized-operation-v1"
AUTHORIZED_OPERATION = "mcd-mautic-json-schema-repair-v1"
_TEXT_TYPES = frozenset({"char", "varchar", "tinytext", "text", "mediumtext", "longtext"})
_COLUMN_BY_KEY = {
    f"{row['table']}.{row['column']}": row for row in KNOWN_JSON_COLUMNS
}
_COLUMN_ORDER = {key: index for index, key in enumerate(_COLUMN_BY_KEY)}


class RepairAuthorizationError(ValueError):
    """Raised when the non-UI MCC operation context cannot be trusted."""


def build_json_repair_plan(table_prefix: str, columns: list[str] | None = None) -> dict[str, Any]:
    """Build the only plan MCD can declare for the known SQLSTATE condition."""
    selected = columns or list(_COLUMN_BY_KEY)
    raw = {
        "schema": JSON_REPAIR_PLAN_CONTRACT,
        "condition": "sqlstate_1253_json_collation_binary",
        "source_major": 6,
        "target_major": 7,
        "table_prefix": table_prefix,
        "columns": sorted(selected, key=lambda item: _COLUMN_ORDER[item]),
        "action": "normalize_declared_json_columns",
    }
    return validate_json_repair_plan(raw)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def repair_plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(plan)).hexdigest()


def _context_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key != "signature"}


def validate_authorization_context(
    raw: str | dict[str, Any],
    *,
    plan: dict[str, Any],
    instance_uid: str,
    root: str,
    source_version: str,
    target_version: str,
    signing_key: bytes,
    now: int | None = None,
) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            context = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RepairAuthorizationError("invalid_authorization_context") from exc
    else:
        context = raw
    if not isinstance(context, dict):
        raise RepairAuthorizationError("authorization_context_not_object")
    required = {
        "schema",
        "operation",
        "instance_uid",
        "root",
        "source_version",
        "target_version",
        "plan_sha256",
        "backup_evidence",
        "issued_at",
        "expires_at",
        "nonce",
        "signature",
    }
    if set(context) != required:
        raise RepairAuthorizationError("authorization_context_keys_not_allowed")
    if context.get("schema") != AUTHORIZATION_CONTRACT or context.get("operation") != AUTHORIZED_OPERATION:
        raise RepairAuthorizationError("authorization_context_contract_mismatch")
    if context.get("instance_uid") != instance_uid or not str(instance_uid or "").strip():
        raise RepairAuthorizationError("authorization_context_instance_mismatch")
    if context.get("root") != root or context.get("source_version") != source_version or context.get("target_version") != target_version:
        raise RepairAuthorizationError("authorization_context_upgrade_mismatch")
    if context.get("plan_sha256") != repair_plan_digest(plan):
        raise RepairAuthorizationError("authorization_context_plan_mismatch")
    backup = context.get("backup_evidence")
    if not isinstance(backup, dict) or set(backup) != {"backup_id", "sha256", "manifest_path", "completed_at", "rollback_supported"}:
        raise RepairAuthorizationError("backup_evidence_missing")
    if not all(str(backup.get(key) or "").strip() for key in ("backup_id", "sha256", "manifest_path", "completed_at")):
        raise RepairAuthorizationError("backup_evidence_incomplete")
    if backup.get("rollback_supported") is not True:
        raise RepairAuthorizationError("backup_rollback_not_supported")
    try:
        issued_at = int(context["issued_at"])
        expires_at = int(context["expires_at"])
    except (TypeError, ValueError) as exc:
        raise RepairAuthorizationError("authorization_context_time_invalid") from exc
    current_time = int(time.time() if now is None else now)
    if issued_at > current_time or expires_at <= current_time or expires_at - issued_at > 3600:
        raise RepairAuthorizationError("authorization_context_expired")
    signature = str(context.get("signature") or "")
    if len(signature) != 64:
        raise RepairAuthorizationError("authorization_context_signature_invalid")
    expected = hmac.new(signing_key, _canonical(_context_payload(context)), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected.lower()):
        raise RepairAuthorizationError("authorization_context_signature_invalid")
    return {
        "schema": AUTHORIZATION_CONTRACT,
        "operation": AUTHORIZED_OPERATION,
        "instance_uid": str(instance_uid),
        "root": str(root),
        "source_version": str(source_version),
        "target_version": str(target_version),
        "plan_sha256": str(context["plan_sha256"]),
        "backup_id": str(backup["backup_id"]),
        "backup_sha256": str(backup["sha256"]),
        "manifest_path": str(backup["manifest_path"]),
        "completed_at": str(backup["completed_at"]),
        "rollback_supported": True,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": str(context["nonce"]),
    }


def load_authorization_context(path: str, *, key_path: str = "/etc/mcd/mcc-operation-signing.key") -> tuple[dict[str, Any], bytes]:
    context_path = Path(path)
    key_file = Path(key_path)
    for candidate in (context_path, key_file):
        info = candidate.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RepairAuthorizationError("authorization_file_permissions_invalid")
        if os.geteuid() == 0 and info.st_uid != 0:
            raise RepairAuthorizationError("authorization_file_owner_invalid")
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
        key = key_file.read_bytes().strip()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RepairAuthorizationError("authorization_context_unavailable") from exc
    if not key:
        raise RepairAuthorizationError("authorization_key_unavailable")
    return context, key


def _read_signing_key(key_path: str) -> bytes:
    path = Path(key_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, secrets.token_bytes(32))
        finally:
            os.close(fd)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (os.geteuid() == 0 and info.st_uid != 0):
        raise RepairAuthorizationError("authorization_key_permissions_invalid")
    key = path.read_bytes().strip()
    if not key:
        raise RepairAuthorizationError("authorization_key_unavailable")
    return key


def verify_backup_evidence(
    backup: dict[str, Any],
    *,
    root: str,
    instance_uid: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify the actual MCD backup marker, not just caller-supplied fields."""
    manifest_path = Path(str(backup.get("manifest_path") or ""))
    if not manifest_path.is_absolute() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise RepairAuthorizationError("backup_manifest_unavailable")
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, str(backup.get("sha256") or "").lower()):
        raise RepairAuthorizationError("backup_manifest_digest_mismatch")
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RepairAuthorizationError("backup_manifest_invalid") from exc
    if marker.get("status") != "ok" or marker.get("backup_id") != backup.get("backup_id"):
        raise RepairAuthorizationError("backup_manifest_not_complete")
    if marker.get("ts_utc") != backup.get("completed_at"):
        raise RepairAuthorizationError("backup_completion_time_mismatch")
    dumped = marker.get("dumped_instances")
    if not isinstance(dumped, list) or not any(
        isinstance(item, dict) and item.get("instance_uid") == instance_uid and item.get("root") == root
        for item in dumped
    ):
        raise RepairAuthorizationError("backup_instance_mismatch")
    completed = marker.get("ts_utc")
    try:
        completed_epoch = int(datetime.fromisoformat(str(completed).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError) as exc:
        raise RepairAuthorizationError("backup_completion_time_invalid") from exc
    if completed_epoch > int(time.time() if now is None else now) or int(time.time() if now is None else now) - completed_epoch > 86400:
        raise RepairAuthorizationError("backup_manifest_too_old")
    return {
        "backup_id": str(backup["backup_id"]),
        "sha256": digest,
        "manifest_path": str(manifest_path),
        "completed_at": str(completed),
        "rollback_supported": True,
    }


def issue_repair_authorization_context(
    *,
    root: str,
    instance_uid: str,
    source_version: str,
    target_version: str,
    repair_plan_json: str | dict[str, Any],
    backup_manifest_path: str,
    key_path: str = "/etc/mcd/mcc-operation-signing.key",
    output_path: str | None = None,
) -> dict[str, Any]:
    plan = validate_json_repair_plan(repair_plan_json)
    marker_path = Path(backup_manifest_path)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        backup = {
            "backup_id": marker.get("backup_id"),
            "sha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            "manifest_path": str(marker_path),
            "completed_at": marker.get("ts_utc"),
            "rollback_supported": True,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RepairAuthorizationError("backup_manifest_unavailable") from exc
    verified = verify_backup_evidence(backup, root=root, instance_uid=instance_uid)
    key = _read_signing_key(key_path)
    now = int(time.time())
    context: dict[str, Any] = {
        "schema": AUTHORIZATION_CONTRACT,
        "operation": AUTHORIZED_OPERATION,
        "instance_uid": instance_uid,
        "root": root,
        "source_version": source_version,
        "target_version": target_version,
        "plan_sha256": repair_plan_digest(plan),
        "backup_evidence": verified,
        "issued_at": now,
        "expires_at": now + 900,
        "nonce": secrets.token_hex(16),
    }
    context["signature"] = hmac.new(key, _canonical(context), hashlib.sha256).hexdigest()
    destination = Path(output_path or f"/run/mcd/mautic-repair-{secrets.token_hex(8)}.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    destination.write_text(json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)
    return {
        "schema": AUTHORIZATION_CONTRACT,
        "status": "authorized",
        "context_path": str(destination),
        "instance_uid": instance_uid,
        "plan_sha256": context["plan_sha256"],
        "backup_id": verified["backup_id"],
        "backup_sha256": verified["sha256"],
        "expires_at": context["expires_at"],
    }


def _identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _column_snapshot(conn: Any, database: str, table: str, column: str) -> dict[str, Any] | None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, COLLATION_NAME, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            [database, table, column],
        )
        row = cursor.fetchone()
    if not row:
        return None
    item = dict(row)
    return {
        "present": True,
        "data_type": str(item.get("DATA_TYPE") or "").lower(),
        "column_type": str(item.get("COLUMN_TYPE") or ""),
        "collation": str(item.get("COLLATION_NAME") or ""),
        "nullable": str(item.get("IS_NULLABLE") or ""),
        "default": None if item.get("COLUMN_DEFAULT") is None else str(item.get("COLUMN_DEFAULT")),
    }


def _json_validity(conn: Any, table: str, column: str) -> dict[str, int]:
    qualified = f"{_identifier(table)}.{_identifier(column)}"
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN {qualified} IS NOT NULL THEN 1 ELSE 0 END) AS non_null, "
            f"SUM(CASE WHEN {qualified} IS NOT NULL AND JSON_VALID({qualified}) THEN 1 ELSE 0 END) AS valid_json, "
            f"SUM(CASE WHEN {qualified} IS NOT NULL AND NOT JSON_VALID({qualified}) THEN 1 ELSE 0 END) AS invalid_json "
            f"FROM {_identifier(table)}"
        )
        row = dict(cursor.fetchone() or {})
    return {key: int(row.get(key) or 0) for key in ("total", "non_null", "valid_json", "invalid_json")}


def _inspect_columns(conn: Any, database: str, prefix: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in sorted(plan["columns"], key=lambda item: _COLUMN_ORDER[item]):
        table_name, column_name = key.split(".", 1)
        expected = _COLUMN_BY_KEY[key]
        table = prefix + table_name
        schema = _column_snapshot(conn, database, table, column_name)
        item: dict[str, Any] = {
            "key": key,
            "table": table,
            "column": column_name,
            "expected_data_type": "json",
            "expected_nullable": bool(expected["nullable"]),
            "before": schema or {"present": False},
        }
        if schema:
            item["before_json_validity"] = _json_validity(conn, table, column_name)
        result.append(item)
    return result


def execute_json_schema_repair(
    *,
    root: str,
    current_version: str,
    target_version: str,
    repair_plan_json: str | dict[str, Any],
    authorization_context: str | dict[str, Any],
    signing_key: bytes,
    instance_uid: str,
    run_id: str,
    local_php_path: str | None = None,
) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence: dict[str, Any] = {
        "schema": "mcd-mautic-json-schema-repair-execution-v1",
        "status": "needs_attention",
        "condition": "sqlstate_1253_json_collation_binary",
        "source_version": str(current_version or ""),
        "target_version": str(target_version or ""),
        "instance_uid": str(instance_uid or ""),
        "run_id": str(run_id or ""),
        "root": str(root),
        "checked_at_utc": checked_at,
        "plan": {"status": "not_validated"},
        "authorization": {"status": "not_validated"},
        "before": [],
        "after": [],
        "applied_columns": [],
        "no_op_columns": [],
        "rollback": {"available": False, "outcome": "not_attempted"},
    }
    if not (str(current_version).startswith("6.") and str(target_version).startswith("7.")):
        evidence.update({"status": "unsupported", "reason": "only Mautic 6 to 7 is supported"})
        return evidence
    try:
        plan = validate_json_repair_plan(repair_plan_json)
    except RepairPlanError as exc:
        evidence["plan"] = {"status": "rejected", "reason": exc.reason}
        evidence["reason"] = "repair plan was rejected"
        return evidence
    evidence["plan"] = {"status": "accepted", "sha256": repair_plan_digest(plan), "action": plan["action"], "columns": plan["columns"]}
    try:
        authorized = validate_authorization_context(
            authorization_context,
            plan=plan,
            instance_uid=instance_uid,
            root=root,
            source_version=current_version,
            target_version=target_version,
            signing_key=signing_key,
        )
    except RepairAuthorizationError as exc:
        evidence["authorization"] = {"status": "rejected", "reason": str(exc)}
        evidence["reason"] = "authorized MCC operation context was rejected"
        return evidence
    try:
        verified_backup = verify_backup_evidence(
            {
                "backup_id": authorized["backup_id"],
                "sha256": authorized["backup_sha256"],
                "manifest_path": authorized["manifest_path"],
                "completed_at": authorized["completed_at"],
                "rollback_supported": authorized["rollback_supported"],
            },
            root=root,
            instance_uid=instance_uid,
        )
    except RepairAuthorizationError as exc:
        evidence["authorization"] = {"status": "rejected", "reason": str(exc)}
        evidence["reason"] = "backup evidence could not be verified"
        return evidence
    evidence["authorization"] = {
        "status": "accepted",
        "backup_id": verified_backup["backup_id"],
        "backup_sha256": verified_backup["sha256"],
        "manifest_path": verified_backup["manifest_path"],
        "completed_at": verified_backup["completed_at"],
        "rollback_supported": True,
    }
    evidence["rollback"] = {"available": True, "mechanism": "authorized_backup_restore", "outcome": "not_attempted"}
    config_path = _local_php_path(root, local_php_path)
    if config_path is None:
        evidence["reason"] = "Mautic local.php was not found"
        return evidence
    try:
        config = parse_local_php(str(config_path))
        prefix = _safe_table_prefix(config.get("db_table_prefix", ""))
        db = DBConfig(
            host=str(config.get("db_host") or "localhost"),
            port=int(config.get("db_port") or 3306),
            name=str(config.get("db_name") or ""),
            user=str(config.get("db_user") or ""),
            password=str(config.get("db_password") or ""),
            table_prefix=prefix,
        )
    except (OSError, RepairPlanError, TypeError, ValueError) as exc:
        evidence["reason"] = "database identity is unavailable"
        evidence["error_type"] = type(exc).__name__
        return evidence
    if plan.get("table_prefix") != prefix:
        evidence["reason"] = "repair plan table prefix does not match the instance"
        return evidence
    try:
        from mcd_agent.mautic_db_indexes import _connect

        with _connect(db) as conn:
            before = _inspect_columns(conn, db.name, prefix, plan)
            evidence["before"] = before
            for item in before:
                schema = item["before"]
                if not schema.get("present"):
                    evidence["reason"] = "declared JSON column is missing"
                    return evidence
                validity = item.get("before_json_validity", {})
                if int(validity.get("invalid_json") or 0) > 0:
                    evidence["reason"] = "declared JSON column contains invalid JSON"
                    return evidence
                expected_nullable = bool(item["expected_nullable"])
                if schema.get("data_type") not in _TEXT_TYPES | {"json"}:
                    evidence["status"] = "unsupported"
                    evidence["reason"] = "declared column type is unsupported"
                    return evidence
                if schema.get("data_type") == "json" and (schema.get("nullable") == ("YES" if expected_nullable else "NO")):
                    evidence["no_op_columns"].append(item["key"])
            for item in before:
                if item["key"] in evidence["no_op_columns"]:
                    continue
                table = _identifier(item["table"])
                column = _identifier(item["column"])
                nullable = "NULL" if item["expected_nullable"] else "NOT NULL"
                with conn.cursor() as cursor:
                    cursor.execute(f"ALTER TABLE {table} MODIFY COLUMN {column} JSON {nullable}")
                evidence["applied_columns"].append(item["key"])
            evidence["after"] = _inspect_columns(conn, db.name, prefix, plan)
            for item in evidence["after"]:
                schema = item["before"]
                expected_nullable = bool(item["expected_nullable"])
                if schema.get("data_type") != "json" or schema.get("nullable") != ("YES" if expected_nullable else "NO"):
                    evidence["status"] = "needs_attention"
                    evidence["reason"] = "post-repair schema verification failed"
                    return evidence
    except Exception as exc:
        evidence["status"] = "needs_attention"
        evidence["reason"] = "JSON schema repair execution failed"
        evidence["error_type"] = type(exc).__name__
        evidence["rollback"]["outcome"] = "available_via_backup"
        return evidence
    evidence["status"] = "success"
    evidence["rollback"]["outcome"] = "not_needed"
    evidence["applied_count"] = len(evidence["applied_columns"])
    evidence["no_op_count"] = len(evidence["no_op_columns"])
    return evidence
