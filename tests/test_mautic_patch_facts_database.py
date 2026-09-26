"""Opt-in acceptance on explicitly provisioned isolated MySQL/MariaDB schemas.

MCD_FACTS_DATABASE_ACCEPTANCE=1 enables this destructive fixture test ONLY for
the exact Operations-owned schema named below. Credentials are Keychain refs,
never argv, source, test output or customer instance credentials.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pytest
import pymysql
from pymysql.cursors import DictCursor

from mcd_agent.discovery import discover_mautic
from mcd_agent.mautic_patch_fact_binding import bound_provider
from mcd_agent.mautic_patch_plan_v3 import PatchPlanV3Error, atomic_preflight, execute


pytestmark = pytest.mark.skipif(os.environ.get("MCD_FACTS_DATABASE_ACCEPTANCE") != "1", reason="explicit isolated database acceptance only")
MIGRATION = "Mautic\\Migrations\\Version20211209022550"
DDL = "Mautic\\Migrations\\Version20260915000000"
SCHEMA = "mcd_typed_predicate_20260926"


def keychain(account):
    service = os.environ["MCD_FACTS_KEYCHAIN_SERVICE"]
    return subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                          capture_output=True, check=True, text=True).stdout.rstrip("\n")


@pytest.fixture(params=["mysql80", "mariadb1011"])
def environment(request):
    engine = request.param
    port = int(os.environ["MCD_FACTS_" + engine.upper() + "_PORT"])
    owner = pymysql.connect(host="127.0.0.1", port=port, user="mcd_fixture_owner", password=keychain(engine + ".fixture_owner"),
                            database=SCHEMA, cursorclass=DictCursor, autocommit=True, connect_timeout=5, read_timeout=10)
    try:
        with tempfile.TemporaryDirectory(prefix="mcd-facts-acceptance-") as temporary:
            root = Path(temporary).resolve()
            for directory in ("app", "plugins", "bin", "config"):
                (root / directory).mkdir()
            (root / "bin/console").write_text("fixture only")
            (root / "composer.lock").write_text('{"packages":[{"name":"mautic/core-lib","version":"7.2.1"}]}')
            password = keychain(engine + ".predicate_ro")
            if "'" in password or "\\" in password:
                pytest.fail("fixture credential cannot be represented by the selected local.php parser")
            local = root / "config/local.php"
            fields = dict(db_host="127.0.0.1", db_port=str(port), db_name=SCHEMA, db_user="mcd_predicate_ro", db_password=password,
                          db_table_prefix="", site_url="https://isolated.invalid")
            def prefix(value):
                fields["db_table_prefix"] = value
                local.write_text("<?php return [" + ",".join("'%s'=>'%s'" % pair for pair in fields.items()) + "];\n")
                local.chmod(0o600)
            prefix("")
            cfg = SimpleNamespace(discovery_roots=[str(root)], exclude_path_contains=[], supported_mautic_majors=[7], custom_instances=[])
            inst = next(item for item in discover_mautic([str(root)], [], [7], []) if item.root == str(root))
            provider = bound_provider(cfg, str(root))
            def reset(roles=(0,), versions=(), table_prefix=""):
                prefix(table_prefix)
                (root / "app/fixture.txt").write_bytes(b"before\n")
                (root / "app/second.txt").write_bytes(b"before\n")
                with owner.cursor() as cursor:
                    for suffix in ("roles", "migrations"):
                        cursor.execute(f"DROP TABLE IF EXISTS `{table_prefix}{suffix}`")
                    cursor.execute(f"CREATE TABLE `{table_prefix}roles` (id INT PRIMARY KEY AUTO_INCREMENT, is_admin TINYINT NULL) ENGINE=InnoDB")
                    cursor.execute(f"CREATE TABLE `{table_prefix}migrations` (version VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci) ENGINE=InnoDB")
                    for value in roles:
                        cursor.execute(f"INSERT INTO `{table_prefix}roles` (is_admin) VALUES (%s)", (value,))
                    for value in versions:
                        cursor.execute(f"INSERT INTO `{table_prefix}migrations` VALUES (%s)", (value,))
            def plan(run_id, table_prefix="", upgrade=False):
                fixture = Path(__file__).parents[1] / "mcd_agent/contracts/fixtures/mautic-patch-resolution-v1.json"
                result = json.loads(fixture.read_text())["resolve_response"]["plan"]
                result.update(source_version="7.1.3" if upgrade else "7.2.1", target_version="7.2.1", run_id=run_id)
                result["execution_context"] = dict(instance_uid=inst.instance_uid, application_root=str(root), table_prefix=table_prefix)
                record = result["patches"][0]
                record["source_paths"] = ["app/fixture.txt"]
                for gate in record["gate"]:
                    gate["path"] = "app/fixture.txt"
                payload = result["payloads"][0]
                data = base64.b64decode(payload["content_base64"]).replace(b"docroot/app/fixture.txt", b"app/fixture.txt")
                payload.update(content_base64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())
                record["preconditions"] = [
                    dict(kind="column_integer_domain", table_suffix="roles", column="is_admin", allowed_values=[0, 1]),
                    dict(kind="table_row_count", table_suffix="roles", filters=[dict(column="is_admin", comparison="equal", value=0)], comparison="greater_than", value=0),
                    dict(kind="migration_execution_state", table_suffix="migrations", version_column="version", migration=MIGRATION, encoding="fqcn_utf8", expected="pending"),
                ]
                record["rollback_preconditions"] = [copy.deepcopy(record["preconditions"][-1])]
                if upgrade:
                    result.update(trigger="upgrade_lifecycle", phase="dependency_update_preflight")
                    record.update(triggers=["upgrade_lifecycle"], phases=["before_cache_warmup"])
                return result
            yield SimpleNamespace(root=root, owner=owner, provider=provider, reset=reset, plan=plan)
    finally:
        owner.close()


@pytest.mark.parametrize("prefix", ["", "test_"])
@pytest.mark.parametrize("roles,versions,decision", [
    ((), (), "skip_condition_not_required"), ((1,), (), "skip_condition_not_required"),
    ((0,), (), "applied"), ((1, 0, 0), (), "applied"), ((0,), (MIGRATION,), "skip_condition_not_required"),
])
def test_verified_selection_and_source_apply(environment, prefix, roles, versions, decision):
    e = environment
    e.reset(roles, versions, prefix)
    p = e.plan("verified-selection", prefix)
    admission = atomic_preflight(str(e.root), p, facts_provider=e.provider)
    result = execute(str(e.root), p, facts_provider=e.provider, accepted_facts=admission["facts_receipt"])
    assert result["patches"][0]["decision"] == decision
    assert (e.root / "app/fixture.txt").read_bytes() == (b"after\n" if decision == "applied" else b"before\n")


def test_admission_fact_and_uid_drift_block_before_source_write(environment):
    e = environment
    e.reset()
    p = e.plan("admission-drift")
    admission = atomic_preflight(str(e.root), p, facts_provider=e.provider)
    with e.owner.cursor() as cursor:
        cursor.execute("INSERT INTO roles (is_admin) VALUES (0)")
    with pytest.raises(PatchPlanV3Error, match="drift"):
        execute(str(e.root), p, facts_provider=e.provider, accepted_facts=admission["facts_receipt"])
    assert (e.root / "app/fixture.txt").read_bytes() == b"before\n"
    p["execution_context"]["instance_uid"] = "foreign"
    with pytest.raises(PatchPlanV3Error, match="binding_mismatch"):
        atomic_preflight(str(e.root), p, facts_provider=e.provider)


def test_pending_reverse_and_executed_barrier(environment):
    e = environment
    for executed in (False, True):
        e.reset()
        p = e.plan("executed-barrier" if executed else "pending-reverse")
        admission = atomic_preflight(str(e.root), p, facts_provider=e.provider)
        execute(str(e.root), p, facts_provider=e.provider, accepted_facts=admission["facts_receipt"])
        if executed:
            with e.owner.cursor() as cursor:
                cursor.execute("INSERT INTO migrations VALUES (%s)", (MIGRATION,))
            with pytest.raises(PatchPlanV3Error, match="database_barrier"):
                execute(str(e.root), dict(p, operation="rollback"), facts_provider=e.provider)
            assert (e.root / "app/fixture.txt").read_bytes() == b"after\n"
        else:
            execute(str(e.root), dict(p, operation="rollback"), facts_provider=e.provider)
            assert (e.root / "app/fixture.txt").read_bytes() == b"before\n"


def test_all_phase_barriers_and_hash_chain_before_any_restore(environment):
    e = environment
    e.reset()
    p = e.plan("all-phase-barrier", upgrade=True)
    second = copy.deepcopy(p["patches"][0])
    second.update(id="SECOND-PHASE-FIXTURE", phase_order=second["phase_order"] + 1, phases=["before_doctrine_migrations"],
                  source_paths=["app/second.txt"], payload_path="fixtures/second.patch")
    for gate in second["gate"]:
        gate["path"] = "app/second.txt"
    second["rollback_preconditions"][0]["migration"] = DDL
    data = base64.b64decode(p["payloads"][0]["content_base64"]).replace(b"app/fixture.txt", b"app/second.txt")
    p["patches"].append(second)
    p["payloads"].append(dict(path="fixtures/second.patch", sha256=hashlib.sha256(data).hexdigest(), content_base64=base64.b64encode(data).decode()))
    admission = atomic_preflight(str(e.root), p, facts_provider=e.provider)
    for phase in ("before_cache_warmup", "before_doctrine_migrations"):
        execute(str(e.root), p, phase=phase, facts_provider=e.provider, accepted_facts=admission["facts_receipt"])
    with e.owner.cursor() as cursor:
        cursor.execute("INSERT INTO migrations VALUES (%s)", (DDL,))
    with pytest.raises(PatchPlanV3Error, match="database_barrier"):
        execute(str(e.root), dict(p, operation="rollback"), facts_provider=e.provider)
    assert (e.root / "app/fixture.txt").read_bytes() == (e.root / "app/second.txt").read_bytes() == b"after\n"
    with e.owner.cursor() as cursor:
        cursor.execute("DELETE FROM migrations")
    (e.root / "app/fixture.txt").write_bytes(b"external drift\n")
    with pytest.raises(PatchPlanV3Error, match="after_hash_mismatch"):
        execute(str(e.root), dict(p, operation="rollback"), facts_provider=e.provider)
    assert (e.root / "app/second.txt").read_bytes() == b"after\n"


@pytest.mark.parametrize("roles,versions,reason", [
    ((None,), (), "domain_unknown"), ((2,), (), "domain_unknown"),
    ((0,), (None,), "encoding_unknown"), ((0,), ("20211209022550",), "encoding_unknown"),
    ((0,), (MIGRATION.lower(),), "case_ambiguous"), ((0,), (MIGRATION, MIGRATION), "cardinality_unknown"),
])
def test_unknown_domain_encoding_and_cardinality_block_before_file_mutation(environment, roles, versions, reason):
    e = environment
    e.reset(roles, versions)
    with pytest.raises(PatchPlanV3Error, match=reason):
        atomic_preflight(str(e.root), e.plan("unknown-facts"), facts_provider=e.provider)
    assert (e.root / "app/fixture.txt").read_bytes() == b"before\n"


@pytest.mark.parametrize("alter,reason", [
    ("ALTER TABLE roles MODIFY COLUMN is_admin VARCHAR(16)", "integer_type_unknown"),
    ("ALTER TABLE roles DROP COLUMN is_admin", "schema_missing"),
    ("DROP TABLE roles", "schema_missing"), ("DROP TABLE migrations", "schema_missing"),
    ("ALTER TABLE migrations CHANGE COLUMN version other_version VARCHAR(191)", "schema_missing"),
    ("ALTER TABLE migrations MODIFY COLUMN version VARCHAR(191) CHARACTER SET latin1", "encoding_unknown"),
])
def test_unverified_database_schema_blocks(environment, alter, reason):
    e = environment
    e.reset()
    with e.owner.cursor() as cursor:
        cursor.execute(alter)
    with pytest.raises(PatchPlanV3Error, match=reason):
        atomic_preflight(str(e.root), e.plan("unknown-schema"), facts_provider=e.provider)
    assert (e.root / "app/fixture.txt").read_bytes() == b"before\n"


@pytest.mark.parametrize("collation", ["utf8mb4_unicode_ci", "utf8mb4_bin"])
def test_exact_migration_identity_is_independent_of_collation(environment, collation):
    e = environment
    for value in (MIGRATION, MIGRATION.lower()):
        e.reset(versions=(value,))
        with e.owner.cursor() as cursor:
            cursor.execute("ALTER TABLE migrations MODIFY COLUMN version VARCHAR(191) CHARACTER SET utf8mb4 COLLATE " + collation)
        p = e.plan("exact-migration")
        if value == MIGRATION:
            result = atomic_preflight(str(e.root), p, facts_provider=e.provider)
            assert result["patches"][0]["decision"] == "skip_condition_not_required"
        else:
            with pytest.raises(PatchPlanV3Error, match="case_ambiguous"):
                atomic_preflight(str(e.root), p, facts_provider=e.provider)


def test_changed_connection_blocks_before_querying_a_different_database(environment):
    e = environment
    e.reset()
    p = e.plan("connection-drift")
    receipt = atomic_preflight(str(e.root), p, facts_provider=e.provider)["facts_receipt"]
    local = e.root / "config/local.php"
    local.write_text(local.read_text().replace("'db_name'=>'" + SCHEMA + "'", "'db_name'=>'unowned_database_do_not_query'"))
    with pytest.raises(PatchPlanV3Error, match="connection_binding_drift"):
        execute(str(e.root), p, facts_provider=e.provider, accepted_facts=receipt)
    assert (e.root / "app/fixture.txt").read_bytes() == b"before\n"
