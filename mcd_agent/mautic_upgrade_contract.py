"""Versioned, read-only contracts for guarded Mautic 6 to 7 upgrades."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from mcd_agent.localphp import parse_local_php
from mcd_agent.models import DBConfig


COMPOSER_CONTRACT = "mcd-mautic-composer-readiness-v1"
UPGRADE_PREFLIGHT_CONTRACT = "mcd-mautic-upgrade-preflight-v1"
JSON_REPAIR_CONTRACT = "mcd-mautic-json-schema-repair-v1"
JSON_REPAIR_PLAN_CONTRACT = "mcd-mautic-json-schema-repair-plan-v1"
COMPOSER_MIN_VERSION = (2, 8, 6)
COMPOSER_BOOTSTRAP_VERSION = "2.8.12"
COMPOSER_PHAR_URL = f"https://getcomposer.org/download/{COMPOSER_BOOTSTRAP_VERSION}/composer.phar"
COMPOSER_SHA256_URL = f"{COMPOSER_PHAR_URL}.sha256sum"

KNOWN_JSON_COLUMNS: tuple[dict[str, Any], ...] = (
    {"table": "dynamic_content", "column": "utm_tags", "nullable": True},
    {"table": "emails", "column": "headers", "nullable": False},
    {"table": "form_fields", "column": "validation", "nullable": True},
    {"table": "form_fields", "column": "conditions", "nullable": True},
    {"table": "imports", "column": "properties", "nullable": True},
    {"table": "lead_event_log", "column": "properties", "nullable": True},
    {"table": "message_channels", "column": "properties", "nullable": False},
    {"table": "reports", "column": "settings", "nullable": True},
    {"table": "sms_message_stats", "column": "details", "nullable": False},
    {"table": "sync_object_mapping", "column": "internal_storage", "nullable": False},
    {"table": "tweet_stats", "column": "response_details", "nullable": True},
)
_KNOWN_COLUMN_KEYS = frozenset(f"{row['table']}.{row['column']}" for row in KNOWN_JSON_COLUMNS)
_TEXT_TYPES = frozenset({"char", "varchar", "tinytext", "text", "mediumtext", "longtext"})


class ComposerReadinessError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = str(status)


class RepairPlanError(ValueError):
    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = str(reason)


def _version_tuple(raw: str) -> tuple[int, int, int]:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", str(raw or ""))
    if not match:
        return (0, 0, 0)
    return tuple(int(match.group(index)) for index in range(1, 4))


def _version_text(value: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in value)


def _probe_version(command: list[str], *, timeout_sec: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, cwd="/", capture_output=True, text=True, timeout=timeout_sec, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, type(exc).__name__
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return int(proc.returncode), output.splitlines()[0].strip() if output else ""


def _composer_path() -> str:
    preferred = _composer_install_path()
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    return str(shutil.which("composer") or "")


def _composer_install_path() -> Path:
    return Path("/usr/local/bin/composer")


def _composer_observation(path: str, version_line: str, *, source: str = "existing") -> dict[str, Any]:
    version = _version_tuple(version_line)
    compatible = bool(path) and version >= COMPOSER_MIN_VERSION
    return {
        "path": str(path or ""),
        "version": str(version_line or ""),
        "version_tuple": list(version),
        "compatible": compatible,
        "minimum_version": _version_text(COMPOSER_MIN_VERSION),
        "source": source,
        "status": "reused" if compatible and source == "existing" else "success" if compatible else "incompatible",
    }


def composer_readiness_from_observation(path: str, version_line: str, *, source: str = "existing") -> dict[str, Any]:
    """Classify a Composer probe without executing or changing the host."""
    if not path:
        return {
            "path": "",
            "version": "",
            "version_tuple": [0, 0, 0],
            "compatible": False,
            "minimum_version": _version_text(COMPOSER_MIN_VERSION),
            "source": source,
            "status": "missing",
        }
    return _composer_observation(path, version_line, source=source)


def _download(url: str, destination: Path) -> None:
    try:
        with urlrequest.urlopen(url, timeout=90) as response:
            content = response.read(32 * 1024 * 1024 + 1)
    except (OSError, urlerror.URLError, TimeoutError) as exc:
        raise ComposerReadinessError("download_failure", "Composer bootstrap download failed") from exc
    if len(content) > 32 * 1024 * 1024:
        raise ComposerReadinessError("download_failure", "Composer bootstrap artifact is too large")
    destination.write_bytes(content)


def _bootstrap_composer(php_bin: str) -> dict[str, Any]:
    php_exec = str(php_bin or "").strip()
    if php_exec and not Path(php_exec).exists():
        php_exec = str(shutil.which(php_exec) or "")
    if not php_exec:
        raise ComposerReadinessError("bootstrap_failure", "configured PHP executable is unavailable")
    install_path = _composer_install_path()
    install_dir = install_path.parent
    if not install_dir.is_dir():
        raise ComposerReadinessError("bootstrap_failure", "Composer install directory is unavailable")
    with tempfile.TemporaryDirectory(prefix="mcd-composer-bootstrap-", dir=str(install_dir)) as temp_dir:
        artifact = Path(temp_dir) / "composer.phar"
        checksum = Path(temp_dir) / "composer.phar.sha256sum"
        _download(COMPOSER_PHAR_URL, artifact)
        try:
            _download(COMPOSER_SHA256_URL, checksum)
            expected_match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum.read_text(encoding="ascii", errors="ignore"))
        except ComposerReadinessError as exc:
            raise ComposerReadinessError("signature_failure", "Composer checksum could not be retrieved") from exc
        if not expected_match:
            raise ComposerReadinessError("signature_failure", "Composer checksum format is invalid")
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual.lower() != expected_match.group(1).lower():
            raise ComposerReadinessError("signature_failure", "Composer checksum verification failed")
        staged = Path(temp_dir) / "composer"
        try:
            with staged.open("wb") as stream:
                stream.write(artifact.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staged, 0o755)
            os.replace(staged, install_path)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ComposerReadinessError("bootstrap_failure", "Composer executable install failed") from exc
    path = str(install_path) if install_path.is_file() else _composer_path()
    if not path:
        raise ComposerReadinessError("bootstrap_failure", "Composer was not present after bootstrap")
    rc, version_line = _probe_version([php_exec, path, "--version", "--no-interaction", "--no-ansi"])
    if rc != 0:
        raise ComposerReadinessError("bootstrap_failure", "Composer version probe failed after bootstrap")
    result = _composer_observation(path, version_line, source="bootstrapped")
    if result["status"] != "success":
        raise ComposerReadinessError("bootstrap_failure", "bootstrapped Composer is incompatible")
    return result


def composer_readiness(*, php_bin: str = "php", allow_bootstrap: bool = False) -> dict[str, Any]:
    """Return sanitized Composer/PHP readiness; bootstrap is opt-in for apply."""
    php_path = str(php_bin or "php").strip() or "php"
    php_rc, php_version = _probe_version([php_path, "-v"])
    php_payload = {
        "path": php_path if php_rc == 0 else "",
        "version": php_version if php_rc == 0 else "",
        "available": php_rc == 0,
    }
    path = _composer_path()
    if path:
        rc, version_line = _probe_version([path, "--version", "--no-interaction", "--no-ansi"])
        result = composer_readiness_from_observation(path, version_line if rc == 0 else "")
        if result["status"] in {"reused", "success"}:
            result["php"] = php_payload
            return result
        if not allow_bootstrap:
            result["php"] = php_payload
            result["bootstrap_available"] = True
            return result
    elif not allow_bootstrap:
        result = composer_readiness_from_observation("", "")
        result["php"] = php_payload
        result["bootstrap_available"] = True
        return result
    try:
        result = _bootstrap_composer(php_path)
    except ComposerReadinessError as exc:
        return {"status": exc.status, "compatible": False, "path": "", "version": "", "php": php_payload}
    result["php"] = php_payload
    return result


def ensure_composer_binary(*, php_bin: str = "php") -> str:
    result = composer_readiness(php_bin=php_bin, allow_bootstrap=True)
    if result.get("status") not in {"reused", "success"}:
        raise ComposerReadinessError(str(result.get("status") or "bootstrap_failure"), "Composer readiness failed")
    return str(result.get("path") or "")


def _safe_table_prefix(raw: str) -> str:
    prefix = str(raw or "")
    if not re.fullmatch(r"[A-Za-z0-9_]*", prefix):
        raise RepairPlanError("invalid_table_prefix", "table prefix contains unsafe characters")
    return prefix


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RepairPlanError("duplicate_key", "repair plan contains a duplicate key")
        result[key] = value
    return result


def validate_json_repair_plan(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > 16_384:
            raise RepairPlanError("plan_too_large")
        try:
            value = json.loads(raw, object_pairs_hook=_unique_json_pairs)
        except RepairPlanError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RepairPlanError("invalid_json") from exc
    else:
        value = raw
    if not isinstance(value, dict):
        raise RepairPlanError("plan_not_object")
    required = {"schema", "condition", "source_major", "target_major", "table_prefix", "columns", "action"}
    if set(value) != required:
        raise RepairPlanError("plan_keys_not_allowed")
    if value.get("schema") != JSON_REPAIR_PLAN_CONTRACT:
        raise RepairPlanError("unsupported_plan_schema")
    if value.get("condition") != "sqlstate_1253_json_collation_binary":
        raise RepairPlanError("unsupported_condition")
    if value.get("source_major") != 6 or value.get("target_major") != 7:
        raise RepairPlanError("unsupported_version_transition")
    prefix = _safe_table_prefix(str(value.get("table_prefix") or ""))
    if value.get("action") != "normalize_declared_json_columns":
        raise RepairPlanError("unsupported_action")
    columns = value.get("columns")
    if not isinstance(columns, list) or not columns:
        raise RepairPlanError("columns_required")
    normalized: list[str] = []
    for item in columns:
        if not isinstance(item, str) or item not in _KNOWN_COLUMN_KEYS or item in normalized:
            raise RepairPlanError("column_not_allowlisted")
        normalized.append(item)
    return {
        "schema": JSON_REPAIR_PLAN_CONTRACT,
        "condition": "sqlstate_1253_json_collation_binary",
        "source_major": 6,
        "target_major": 7,
        "table_prefix": prefix,
        "columns": normalized,
        "action": "normalize_declared_json_columns",
    }


def _local_php_path(root: str, supplied: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if supplied:
        candidates.append(Path(supplied))
    base = Path(root)
    candidates.extend([base / "config" / "local.php", base / "app" / "config" / "local.php"])
    if base.name.lower() in {"public", "docroot", "public_html"}:
        candidates.extend([base.parent / "config" / "local.php", base.parent / "app" / "config" / "local.php"])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _schema_columns(conn: Any, database: str, table: str, columns: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(columns))
    query = (
        "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, COLLATION_NAME, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        f"AND COLUMN_NAME IN ({placeholders}) ORDER BY ORDINAL_POSITION"
    )
    with conn.cursor() as cursor:
        cursor.execute(query, [database, table, *columns])
        return [dict(row) for row in cursor.fetchall()]


def _json_validity(conn: Any, table: str, column: str, data_type: str) -> dict[str, Any]:
    identifier = "`" + table.replace("`", "``") + "`.`" + column.replace("`", "``") + "`"
    query = (
        f"SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN {identifier} IS NOT NULL THEN 1 ELSE 0 END) AS non_null, "
        f"SUM(CASE WHEN {identifier} IS NOT NULL AND JSON_VALID({identifier}) THEN 1 ELSE 0 END) AS valid_json, "
        f"SUM(CASE WHEN {identifier} IS NOT NULL AND NOT JSON_VALID({identifier}) THEN 1 ELSE 0 END) AS invalid_json "
        f"FROM `{table.replace('`', '``')}`"
    )
    with conn.cursor() as cursor:
        cursor.execute(query)
        row = dict(cursor.fetchone() or {})
    return {
        "data_type": data_type,
        "total": int(row.get("total") or 0),
        "non_null": int(row.get("non_null") or 0),
        "valid_json": int(row.get("valid_json") or 0),
        "invalid_json": int(row.get("invalid_json") or 0),
    }


def inspect_json_schema_repair(
    *,
    root: str,
    current_version: str,
    target_version: str,
    local_php_path: str | None = None,
    repair_plan_json: str | None = None,
    backup_confirmed: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": JSON_REPAIR_CONTRACT,
        "status": "unsupported",
        "condition": "sqlstate_1253_json_collation_binary",
        "source_version": str(current_version or ""),
        "target_version": str(target_version or ""),
        "table_prefix": "",
        "database": {"server_version": "", "engine": "", "version_tuple": [0, 0, 0]},
        "affected_columns": [],
        "json_validity": {"status": "not_checked", "columns": []},
        "backup_prerequisite": {
            "required": True,
            "satisfied": bool(backup_confirmed),
            "source": "mcc_backup_evidence" if backup_confirmed else "not_confirmed",
        },
        "repair_plan": {"status": "not_supplied"},
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if _version_tuple(current_version)[0] != 6 or _version_tuple(target_version)[0] != 7:
        payload["reason"] = "only Mautic 6 to 7 is supported"
        return payload
    if repair_plan_json:
        try:
            payload["repair_plan"] = {"status": "accepted", "plan": validate_json_repair_plan(repair_plan_json)}
        except RepairPlanError as exc:
            payload["repair_plan"] = {"status": "rejected", "reason": exc.reason}

    config_path = _local_php_path(root, local_php_path)
    if config_path is None:
        payload.update({"status": "needs_attention", "reason": "Mautic local.php was not found"})
        return payload
    try:
        config = parse_local_php(str(config_path))
        prefix = _safe_table_prefix(config.get("db_table_prefix", ""))
    except (OSError, RepairPlanError) as exc:
        payload.update({"status": "needs_attention", "reason": "database identity is unavailable"})
        return payload
    payload["table_prefix"] = prefix
    accepted_plan = payload.get("repair_plan", {})
    plan_value = accepted_plan.get("plan") if isinstance(accepted_plan, dict) else None
    if isinstance(plan_value, dict) and plan_value.get("table_prefix") != prefix:
        payload.update({"status": "needs_attention", "reason": "repair plan table prefix does not match the instance"})
        return payload
    required_db_keys = ("db_name", "db_user")
    if not all(str(config.get(key) or "").strip() for key in required_db_keys):
        payload.update({"status": "needs_attention", "reason": "database credentials are incomplete"})
        return payload
    db = DBConfig(
        host=str(config.get("db_host") or "localhost"),
        port=int(config.get("db_port") or 3306),
        name=str(config.get("db_name") or ""),
        user=str(config.get("db_user") or ""),
        password=str(config.get("db_password") or ""),
        table_prefix=prefix,
    )
    try:
        from mcd_agent.mautic_db_indexes import _connect

        with _connect(db) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version")
                server_version = str((cursor.fetchone() or {}).get("version") or "")
            engine = "mariadb" if "mariadb" in server_version.lower() else "mysql"
            payload["database"] = {
                "server_version": server_version,
                "engine": engine,
                "version_tuple": list(_version_tuple(server_version)),
                "name": db.name,
            }
            observed: list[dict[str, Any]] = []
            validity: list[dict[str, Any]] = []
            for group in ("dynamic_content", "emails", "form_fields", "imports", "lead_event_log", "message_channels", "reports", "sms_message_stats", "sync_object_mapping", "tweet_stats"):
                expected = [row for row in KNOWN_JSON_COLUMNS if row["table"] == group]
                table = prefix + group
                rows = _schema_columns(conn, db.name, table, [str(row["column"]) for row in expected])
                by_name = {str(row.get("COLUMN_NAME")): row for row in rows}
                for row in expected:
                    column = str(row["column"])
                    current = by_name.get(column)
                    item = {
                        "table": table,
                        "column": column,
                        "expected_data_type": "json",
                        "expected_nullable": bool(row["nullable"]),
                        "observed": {
                            "present": bool(current),
                            "data_type": str((current or {}).get("DATA_TYPE") or ""),
                            "column_type": str((current or {}).get("COLUMN_TYPE") or ""),
                            "collation": str((current or {}).get("COLLATION_NAME") or ""),
                            "nullable": str((current or {}).get("IS_NULLABLE") or ""),
                        },
                    }
                    observed.append(item)
                    if current:
                        validity.append({"table": table, "column": column, **_json_validity(conn, table, column, str(current.get("DATA_TYPE") or ""))})
            payload["affected_columns"] = observed
            payload["json_validity"] = {"status": "checked", "columns": validity}
    except Exception as exc:
        payload.update({"status": "needs_attention", "reason": "read-only database inspection failed", "error_type": type(exc).__name__})
        return payload

    if payload["repair_plan"].get("status") == "rejected":
        payload.update({"status": "needs_attention", "reason": "repair plan was rejected"})
        return payload
    rows = payload["affected_columns"]
    valid_rows = payload["json_validity"]["columns"]
    if len(rows) != len(KNOWN_JSON_COLUMNS) or any(not row["observed"]["present"] for row in rows):
        payload.update({"status": "needs_attention", "reason": "known JSON migration columns are missing"})
        return payload
    if any(int(row.get("invalid_json") or 0) > 0 for row in valid_rows):
        payload.update({"status": "needs_attention", "reason": "affected columns contain invalid JSON"})
        return payload
    if any(str(row["observed"]["data_type"]).lower() not in {"json", *_TEXT_TYPES} for row in rows):
        payload.update({"status": "needs_attention", "reason": "affected column type is outside the declared compatibility set"})
        return payload
    payload.update({"status": "supported", "reason": "known 6 to 7 JSON schema condition is safely diagnosable"})
    return payload
