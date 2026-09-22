from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcd_agent.mautic_upgrade_contract import (
    COMPOSER_MIN_VERSION,
    JSON_REPAIR_PLAN_CONTRACT,
    ComposerReadinessError,
    composer_readiness_from_observation,
    composer_readiness,
    _bootstrap_composer,
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

    def test_verified_phar_is_installed_as_runnable_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_path = Path(temp_dir) / "composer"
            artifact = b"verified-composer-phar"
            download_parents: list[Path] = []

            def download(url: str, destination: Path) -> None:
                download_parents.append(destination.parent)
                if url.endswith("sha256sum"):
                    destination.write_text(hashlib.sha256(artifact).hexdigest() + "  composer.phar\n", encoding="ascii")
                else:
                    destination.write_bytes(artifact)

            with patch("mcd_agent.mautic_upgrade_contract._composer_install_path", return_value=install_path), patch(
                "mcd_agent.mautic_upgrade_contract._download", side_effect=download
            ), patch(
                "mcd_agent.mautic_upgrade_contract._probe_version", return_value=(0, "Composer version 2.9.5")
            ):
                result = _bootstrap_composer("/bin/sh")
            self.assertEqual(result["status"], "success")
            self.assertEqual(install_path.read_bytes(), artifact)
            self.assertTrue(install_path.stat().st_mode & 0o111)
            self.assertTrue(download_parents)
            self.assertTrue(all(parent.parent == Path(temp_dir) for parent in download_parents))


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

    def test_repair_plan_prefix_must_match_instance(self) -> None:
        plan = {
            "schema": JSON_REPAIR_PLAN_CONTRACT,
            "condition": "sqlstate_1253_json_collation_binary",
            "source_major": 6,
            "target_major": 7,
            "table_prefix": "ss_",
            "columns": ["emails.headers"],
            "action": "normalize_declared_json_columns",
        }
        with patch("mcd_agent.mautic_upgrade_contract._local_php_path", return_value=Path("/fixture/config/local.php")), patch(
            "mcd_agent.mautic_upgrade_contract.parse_local_php",
            return_value={"db_table_prefix": "other_", "db_name": "fixture", "db_user": "fixture"},
        ):
            result = inspect_json_schema_repair(
                root="/var/www/fixture",
                current_version="6.0.7",
                target_version="7.1.3",
                repair_plan_json=plan,
            )
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["reason"], "repair plan table prefix does not match the instance")


if __name__ == "__main__":
    unittest.main()
