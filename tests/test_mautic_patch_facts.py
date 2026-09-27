import copy
import pytest

from mcd_agent.mautic_patch_facts import (
    PatchFactsError, bind_evidence, digest, migration_state, require_unchanged, validate_predicates,
)


MIGRATION = "Mautic\\Migrations\\Version20211209022550"


def predicates():
    return [
        {"kind": "column_integer_domain", "table_suffix": "roles", "column": "is_admin", "allowed_values": [0, 1]},
        {"kind": "table_row_count", "table_suffix": "roles", "filters": [{"column": "is_admin", "comparison": "equal", "value": 0}], "comparison": "greater_than", "value": 0},
        {"kind": "migration_execution_state", "table_suffix": "migrations", "version_column": "version", "migration": MIGRATION, "encoding": "fqcn_utf8", "expected": "pending"},
    ]


def test_filtered_count_requires_explicit_integer_domain():
    rows = predicates()
    validate_predicates(rows)
    validate_predicates(list(reversed(rows)))
    with pytest.raises(PatchFactsError, match="domain_required"):
        validate_predicates(rows[1:])


@pytest.mark.parametrize("value", [True, "0", None, 0.0])
def test_filter_values_are_typed_integers(value):
    rows = predicates()
    rows[1]["filters"][0]["value"] = value
    with pytest.raises(PatchFactsError, match="filter_invalid"):
        validate_predicates(rows)


@pytest.mark.parametrize("values", [[1, 0], [0, 0], [False, 1], [], ["0", "1"]])
def test_domain_is_sorted_unique_bounded_integers(values):
    rows = predicates()
    rows[0]["allowed_values"] = values
    with pytest.raises(PatchFactsError, match="domain_values_invalid"):
        validate_predicates(rows)


def test_migration_exact_cardinality_is_independent_of_database_collation():
    assert migration_state([], MIGRATION) == ("pending", 0)
    assert migration_state([MIGRATION], MIGRATION) == ("executed", 1)
    assert migration_state(["Mautic\\Migrations\\Version20260915000000"], MIGRATION) == ("pending", 0)
    with pytest.raises(PatchFactsError, match="cardinality_unknown"):
        migration_state([MIGRATION, MIGRATION], MIGRATION)


@pytest.mark.parametrize("value", [None, 20211209022550, MIGRATION.encode(), ""])
def test_migration_unknown_encoding_never_means_pending(value):
    with pytest.raises(PatchFactsError, match="encoding_unknown"):
        migration_state([value], MIGRATION)


@pytest.mark.parametrize("values,expected", [
    (["20160101000000", MIGRATION, "Mautic\\Migrations\\Version20260915000000"], ("executed", 1)),
    (["20160101000000", "Mautic\\Migrations\\Version20260915000000"], ("pending", 0)),
    (["20240229010203", MIGRATION], ("executed", 1)),
])
def test_documented_mixed_storage_keeps_exact_target_and_raw_hash(values, expected):
    before = copy.deepcopy(values); raw_hash = digest(values)
    assert migration_state(values, MIGRATION) == expected
    assert values == before and digest(values) == raw_hash


@pytest.mark.parametrize("alias", ["20211209022550", "Version20211209022550",
    "Other\\Migrations\\Version20211209022550", "Other\\Migrations\\version20211209022550"])
@pytest.mark.parametrize("exact_present", [False, True])
def test_target_alias_never_proves_pending_or_executed(alias, exact_present):
    with pytest.raises(PatchFactsError, match="alias_ambiguous"):
        migration_state(([MIGRATION] if exact_present else []) + [alias], MIGRATION)


@pytest.mark.parametrize("value", ["20230229000000", "20241301000000", "20240101240000",
    "00000101000000", "2016010100000", "201601010000000", " 20160101000000",
    "Version20160101000000", "malformed", "\\udcff", "Mautic\\Bad-Name"])
def test_unknown_unrelated_storage_still_rejects_exact_target(value):
    with pytest.raises(PatchFactsError, match="encoding_unknown"):
        migration_state([MIGRATION, value], MIGRATION)


@pytest.mark.parametrize("duplicate", ["20160101000000", "Other\\Version20160101000000", MIGRATION])
def test_duplicate_storage_is_not_silently_normalized(duplicate):
    with pytest.raises(PatchFactsError, match="cardinality_unknown"):
        migration_state([duplicate, duplicate], MIGRATION)


def test_complete_capture_limit_and_target_encoding_are_still_required():
    with pytest.raises(PatchFactsError, match="storage_limit"):
        migration_state(["20160101000000"] * 10001, MIGRATION)
    with pytest.raises(PatchFactsError, match="target_encoding_unknown"):
        migration_state([], "20211209022550")


def test_case_folded_near_match_blocks_instead_of_claiming_pending():
    with pytest.raises(PatchFactsError, match="case_ambiguous"):
        migration_state([MIGRATION.lower()], MIGRATION)


@pytest.mark.parametrize("field,value", [("table_suffix", "roles;DROP TABLE roles"), ("table_suffix", "../roles")])
def test_catalog_cannot_supply_sql_or_path_expressions(field, value):
    rows = predicates()
    rows[0][field] = value
    with pytest.raises(PatchFactsError, match="identifier_invalid"):
        validate_predicates(rows)


def test_digest_binds_nonsecret_context_and_typed_observations():
    context = {"instance_uid": "fixture-1", "application_root": "/isolated/app", "table_prefix": "test_",
               "source_version": "6.0.9", "target_version": "7.2.1", "run_id": "fixture-run",
               "plan_sha256": "a" * 64, "trigger": "upgrade_lifecycle", "phase": "before_doctrine_migrations", "operation": "apply"}
    observation = {"schema": "mcd-mautic-patch-facts-v1", "table_prefix": "test_",
                   "database_identity_sha256": "b" * 64, "facts": [{"observed": 1, "matched": True}]}
    evidence = bind_evidence(observation, context)
    require_unchanged(evidence, copy.deepcopy(evidence))
    changed = copy.deepcopy(evidence)
    changed["execution_context"]["instance_uid"] = "other"
    with pytest.raises(PatchFactsError, match="drift"):
        require_unchanged(evidence, changed)
    tampered = copy.deepcopy(evidence)
    tampered["facts_sha256"] = "0" * 64
    with pytest.raises(PatchFactsError, match="drift"):
        require_unchanged(tampered, tampered)
    assert digest({"value": 1}) != digest({"value": "1"})
    assert digest({"a": 1, "b": None}) == digest({"b": None, "a": 1})


def test_no_unknown_fields_or_unbounded_database_fact_types():
    rows = predicates()
    rows[2]["sql"] = "SELECT * FROM migrations"
    with pytest.raises(PatchFactsError, match="migration_fields_invalid"):
        validate_predicates(rows)
