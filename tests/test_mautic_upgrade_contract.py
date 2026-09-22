from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mcd_agent.mautic_upgrade_contract import (
    COMPOSER_MIN_VERSION,
    JSON_REPAIR_PLAN_CONTRACT,
    ComposerReadinessError,
    composer_readiness_from_observation,
    composer_readiness,
    inspect_json_schema_repair,
    validate_json_repair_plan,
)


class ComposerReadinessContractTests(unittest.TestCase):
    def test_missing_is_distinct_and_fail_closed(self) -> None:
        result = composer_readiness_from_observation("", "")
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result["compatible"])

    def test_incompatible_version_is_distinct(self) -> None:
        result = composer_readiness_from_observation("/usr/local/bin/composer", "Composer version 2.8.5")
        self.assertEqual(result["status"], "incompatible")
        self.assertEqual(result["minimum_version"], ".".join(map(str, COMPOSER_MIN_VERSION)))

    def test_compatible_existing_composer_is_reused(self) -> None:
        result = composer_readiness_from_observation("/usr/local/bin/composer", "Composer version 2.9.5")
        self.assertEqual(result["status"], "reused")
        self.assertTrue(result["compatible"])

    def test_bootstrap_failures_are_distinct(self) -> None:
        for status in ("download_failure", "signature_failure", "bootstrap_failure"):
            with self.subTest(status=status), patch(
                "mcd_agent.mautic_upgrade_contract._composer_path", return_value=""
            ), patch(
                "mcd_agent.mautic_upgrade_contract._bootstrap_composer",
                side_effect=ComposerReadinessError(status, status),
            ):
                result = composer_readiness(php_bin="/usr/bin/php", allow_bootstrap=True)
            self.assertEqual(result["status"], status)
            self.assertFalse(result["compatible"])


class JsonRepairPlanContractTests(unittest.TestCase):
    def test_accepts_only_declared_columns(self) -> None:
        plan = validate_json_repair_plan(
            json.dumps(
                {
                    "schema": JSON_REPAIR_PLAN_CONTRACT,
                    "condition": "sqlstate_1253_json_collation_binary",
                    "source_major": 6,
                    "target_major": 7,
                    "table_prefix": "ss_",
                    "columns": ["dynamic_content.utm_tags", "emails.headers"],
                    "action": "normalize_declared_json_columns",
                }
            )
        )
        self.assertEqual(plan["table_prefix"], "ss_")

    def test_rejects_sql_and_unknown_columns(self) -> None:
        base = {
            "schema": JSON_REPAIR_PLAN_CONTRACT,
            "condition": "sqlstate_1253_json_collation_binary",
            "source_major": 6,
            "target_major": 7,
            "table_prefix": "ss_",
            "columns": ["dynamic_content.utm_tags"],
            "action": "normalize_declared_json_columns",
        }
        with self.assertRaisesRegex(ValueError, "plan_keys_not_allowed"):
            validate_json_repair_plan({**base, "sql": "ALTER TABLE ..."})
        with self.assertRaisesRegex(ValueError, "column_not_allowlisted"):
            validate_json_repair_plan({**base, "columns": ["users.password"]})


class JsonRepairInspectionTests(unittest.TestCase):
    def test_non_mautic_6_to_7_transition_is_unsupported_without_db_access(self) -> None:
        result = inspect_json_schema_repair(
            root="/missing",
            current_version="7.1.3",
            target_version="7.2.0",
        )
        self.assertEqual(result["status"], "unsupported")

    def test_missing_local_config_is_needs_attention(self) -> None:
        result = inspect_json_schema_repair(
            root="/missing",
            current_version="6.0.7",
            target_version="7.1.3",
        )
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["reason"], "Mautic local.php was not found")


if __name__ == "__main__":
    unittest.main()
