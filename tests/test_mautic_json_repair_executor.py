from __future__ import annotations

import hashlib
import hmac
import json
import re
import tempfile
import time
from datetime import datetime, timezone
import unittest
from pathlib import Path
from unittest.mock import patch

from mcd_agent.mautic_json_repair import (
    AUTHORIZATION_CONTRACT,
    AUTHORIZED_OPERATION,
    execute_json_schema_repair,
    issue_repair_authorization_context,
    load_authorization_context,
    repair_plan_digest,
    validate_authorization_context,
)
from mcd_agent.mautic_upgrade_contract import JSON_REPAIR_PLAN_CONTRACT, KNOWN_JSON_COLUMNS, validate_json_repair_plan


def _plan(columns: list[str] | None = None) -> dict[str, object]:
    return {
        "schema": JSON_REPAIR_PLAN_CONTRACT,
        "condition": "sqlstate_1253_json_collation_binary",
        "source_major": 6,
        "target_major": 7,
        "table_prefix": "ss_",
        "columns": columns or [f"{row['table']}.{row['column']}" for row in KNOWN_JSON_COLUMNS],
        "action": "normalize_declared_json_columns",
    }


def _signed_context(plan: dict[str, object], key: bytes, *, instance_uid: str = "fixture") -> dict[str, object]:
    now = int(time.time())
    context: dict[str, object] = {
        "schema": AUTHORIZATION_CONTRACT,
        "operation": AUTHORIZED_OPERATION,
        "instance_uid": instance_uid,
        "root": "/var/www/fixture",
        "source_version": "6.0.7",
        "target_version": "7.1.3",
        "plan_sha256": repair_plan_digest(validate_json_repair_plan(plan)),
        "backup_evidence": {
            "backup_id": "backup-fixture-1",
            "sha256": "a" * 64,
            "manifest_path": "/fixture/backup",
            "completed_at": "2026-09-22T10:00:00Z",
            "rollback_supported": True,
        },
        "issued_at": now - 1,
        "expires_at": now + 300,
        "nonce": "nonce-fixture-1",
    }
    context["signature"] = hmac.new(
        key,
        json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return context


class _Cursor:
    def __init__(self, state: dict[str, dict[str, object]], statements: list[str]) -> None:
        self.state = state
        self.statements = statements
        self.row = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: list[object] | None = None) -> None:
        self.statements.append(query)
        if query.startswith("ALTER TABLE"):
            match = re.search(r"`([^`]+)` MODIFY COLUMN `([^`]+)` JSON (NULL|NOT NULL)", query)
            assert match is not None
            key = match.group(1).removeprefix("ss_") + "." + match.group(2)
            self.state[key]["data_type"] = "json"
            self.state[key]["nullable"] = "YES" if match.group(3) == "NULL" else "NO"

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class _Connection:
    def __init__(self, state: dict[str, dict[str, object]], statements: list[str]) -> None:
        self.state = state
        self.statements = statements

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.state, self.statements)


class JsonRepairExecutorTests(unittest.TestCase):
    def test_all_nullable_and_non_nullable_fields_execute_in_canonical_order(self) -> None:
        plan = _plan()
        key = b"fixture-signing-key"
        state = {
            f"{row['table']}.{row['column']}": {
                "present": True,
                "data_type": "text",
                "column_type": "text",
                "collation": "utf8mb4_bin",
                "nullable": "YES" if row["nullable"] else "NO",
                "default": None,
            }
            for row in KNOWN_JSON_COLUMNS
        }
        statements: list[str] = []
        connection = _Connection(state, statements)

        def snapshot(_conn: object, _database: str, table: str, column: str) -> dict[str, object]:
            return dict(state[table.removeprefix("ss_") + "." + column])

        with patch("mcd_agent.mautic_json_repair._local_php_path", return_value=Path("/fixture/config/local.php")), patch(
            "mcd_agent.mautic_json_repair.parse_local_php",
            return_value={"db_table_prefix": "ss_", "db_name": "fixture", "db_user": "fixture"},
        ), patch("mcd_agent.mautic_db_indexes._connect", return_value=connection), patch(
            "mcd_agent.mautic_json_repair._column_snapshot", side_effect=snapshot
        ), patch(
            "mcd_agent.mautic_json_repair._json_validity",
            return_value={"total": 4, "non_null": 4, "valid_json": 4, "invalid_json": 0},
        ), patch(
            "mcd_agent.mautic_json_repair.verify_backup_evidence",
            return_value={"backup_id": "backup-fixture-1", "sha256": "a" * 64, "manifest_path": "/fixture/backup", "completed_at": "2026-09-22T10:00:00Z", "rollback_supported": True},
        ):
            result = execute_json_schema_repair(
                root="/var/www/fixture",
                current_version="6.0.7",
                target_version="7.1.3",
                repair_plan_json=plan,
                authorization_context=_signed_context(plan, key),
                signing_key=key,
                instance_uid="fixture",
                run_id="repair-fixture-1",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied_count"], len(KNOWN_JSON_COLUMNS))
        self.assertEqual(result["no_op_count"], 0)
        self.assertEqual(result["run_id"], "repair-fixture-1")
        self.assertEqual(len([sql for sql in statements if sql.startswith("ALTER TABLE")]), len(KNOWN_JSON_COLUMNS))
        self.assertEqual(result["rollback"]["outcome"], "not_needed")

    def test_already_normalized_nullable_and_non_nullable_fields_are_noop(self) -> None:
        keys = ["dynamic_content.utm_tags", "emails.headers"]
        plan = _plan(keys)
        key = b"fixture-signing-key"
        state = {
            keys[0]: {"present": True, "data_type": "json", "column_type": "json", "collation": "", "nullable": "YES", "default": None},
            keys[1]: {"present": True, "data_type": "json", "column_type": "json", "collation": "", "nullable": "NO", "default": None},
        }
        statements: list[str] = []
        connection = _Connection(state, statements)

        def snapshot(_conn: object, _database: str, table: str, column: str) -> dict[str, object]:
            return dict(state[table.removeprefix("ss_") + "." + column])

        with patch("mcd_agent.mautic_json_repair._local_php_path", return_value=Path("/fixture/config/local.php")), patch(
            "mcd_agent.mautic_json_repair.parse_local_php",
            return_value={"db_table_prefix": "ss_", "db_name": "fixture", "db_user": "fixture"},
        ), patch("mcd_agent.mautic_db_indexes._connect", return_value=connection), patch(
            "mcd_agent.mautic_json_repair._column_snapshot", side_effect=snapshot
        ), patch(
            "mcd_agent.mautic_json_repair._json_validity",
            return_value={"total": 1, "non_null": 1, "valid_json": 1, "invalid_json": 0},
        ), patch(
            "mcd_agent.mautic_json_repair.verify_backup_evidence",
            return_value={"backup_id": "backup-fixture-1", "sha256": "a" * 64, "manifest_path": "/fixture/backup", "completed_at": "2026-09-22T10:00:00Z", "rollback_supported": True},
        ):
            result = execute_json_schema_repair(
                root="/var/www/fixture",
                current_version="6.0.7",
                target_version="7.1.3",
                repair_plan_json=plan,
                authorization_context=_signed_context(plan, key),
                signing_key=key,
                instance_uid="fixture",
                run_id="repair-fixture-noop",
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["applied_count"], 0)
        self.assertEqual(result["no_op_count"], 2)
        self.assertFalse([sql for sql in statements if sql.startswith("ALTER TABLE")])

    def test_invalid_json_stops_before_any_mutation(self) -> None:
        plan = _plan(["dynamic_content.utm_tags"])
        key = b"fixture-signing-key"
        connection = _Connection({}, [])
        with patch("mcd_agent.mautic_json_repair._local_php_path", return_value=Path("/fixture/config/local.php")), patch(
            "mcd_agent.mautic_json_repair.parse_local_php",
            return_value={"db_table_prefix": "ss_", "db_name": "fixture", "db_user": "fixture"},
        ), patch("mcd_agent.mautic_db_indexes._connect", return_value=connection), patch(
            "mcd_agent.mautic_json_repair._column_snapshot",
            return_value={"present": True, "data_type": "text", "column_type": "text", "collation": "utf8mb4_bin", "nullable": "YES", "default": None},
        ), patch(
            "mcd_agent.mautic_json_repair._json_validity",
            return_value={"total": 1, "non_null": 1, "valid_json": 0, "invalid_json": 1},
        ), patch(
            "mcd_agent.mautic_json_repair.verify_backup_evidence",
            return_value={"backup_id": "backup-fixture-1", "sha256": "a" * 64, "manifest_path": "/fixture/backup", "completed_at": "2026-09-22T10:00:00Z", "rollback_supported": True},
        ):
            result = execute_json_schema_repair(
                root="/var/www/fixture",
                current_version="6.0.7",
                target_version="7.1.3",
                repair_plan_json=plan,
                authorization_context=_signed_context(plan, key),
                signing_key=key,
                instance_uid="fixture",
                run_id="repair-fixture-invalid",
            )
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["reason"], "declared JSON column contains invalid JSON")
        self.assertFalse(connection.statements)

    def test_context_signature_and_backup_are_mandatory(self) -> None:
        plan = validate_json_repair_plan(_plan(["emails.headers"]))
        key = b"fixture-signing-key"
        context = _signed_context(plan, key)
        context["backup_evidence"] = {"backup_id": "x"}
        with self.assertRaisesRegex(ValueError, "backup_evidence_missing"):
            validate_authorization_context(
                context,
                plan=plan,
                instance_uid="fixture",
                root="/var/www/fixture",
                source_version="6.0.7",
                target_version="7.1.3",
                signing_key=key,
            )
        context = _signed_context(plan, key)
        context["signature"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "authorization_context_signature_invalid"):
            validate_authorization_context(
                context,
                plan=plan,
                instance_uid="fixture",
                root="/var/www/fixture",
                source_version="6.0.7",
                target_version="7.1.3",
                signing_key=key,
            )

    def test_issue_load_validate_round_trip_uses_real_backup_manifest(self) -> None:
        plan = _plan(["emails.headers"])
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            completed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            marker = {
                "status": "ok",
                "backup_id": "fixture:20260922-120000",
                "ts_utc": completed_at,
                "dumped_instances": [{"instance_uid": "fixture", "root": "/var/www/fixture"}],
            }
            manifest = temp / ".mcd-backup.json"
            manifest.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
            key_path = temp / "mcc-operation-signing.key"
            context_path = temp / "repair-context.json"
            result = issue_repair_authorization_context(
                root="/var/www/fixture",
                instance_uid="fixture",
                source_version="6.0.7",
                target_version="7.1.3",
                repair_plan_json=plan,
                backup_manifest_path=str(manifest),
                key_path=str(key_path),
                output_path=str(context_path),
            )
            self.assertEqual(result["status"], "authorized")
            context, signing_key = load_authorization_context(str(context_path), key_path=str(key_path))
            validated = validate_authorization_context(
                context,
                plan=validate_json_repair_plan(plan),
                instance_uid="fixture",
                root="/var/www/fixture",
                source_version="6.0.7",
                target_version="7.1.3",
                signing_key=signing_key,
            )
            self.assertEqual(validated["backup_id"], "fixture:20260922-120000")
            self.assertEqual(validated["manifest_path"], str(manifest))
if __name__ == "__main__":
    unittest.main()
