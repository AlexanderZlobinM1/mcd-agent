import json

import pytest

from mcd_agent.mautic_patch_facts import PatchFactsError, migration_state

TARGET = "Fixture\\Migrations\\Version20211209022550"


@pytest.mark.parametrize("value,category", [
    (None, "null"), (42, "non_string"), (True, "non_string"), (b"private", "non_string"),
    ("", "empty"), ("x" * 1025, "oversize"), ("\udcff", "invalid_utf8"), ("privat\u00e9", "non_ascii"),
    (" private", "whitespace"), ("20230229000000", "invalid_calendar"), ("12345", "numeric_other"),
    ("Secret\\\\Identifier", "repeated_separator"), ("\\Secret\\Identifier", "leading_namespace"),
    ("Secret\\Identifier\\", "trailing_namespace"), ("Secret/Identifier", "slash_separator"),
    ("PrivateIdentifier", "unqualified_identifier"), ("private-value", "other"),
])
def test_unknown_storage_diagnostic_is_bounded_and_does_not_leak(value, category):
    values = ["Other\\Migrations\\Version20150101000000", "20160101000000", value]
    with pytest.raises(PatchFactsError) as error:
        migration_state(values, TARGET)
    reason = str(error.value)
    assert reason.startswith("fact_migration_encoding_unknown;diagnostic=")
    assert reason.isascii() and len(reason.encode()) <= 512
    diagnostic = json.loads(reason.split(";diagnostic=", 1)[1])
    assert diagnostic["category"] == category
    assert diagnostic["rows"] == 3 and diagnostic["fqcn"] == 1 and diagnostic["legacy"] == 1
    assert diagnostic["unknown"] == 1 and diagnostic["same"] == 1
    assert diagnostic["v"] == 2 and diagnostic["exact"] == 0
    assert TARGET not in reason and "Secret" not in reason and "private" not in reason.lower()


def test_duplicate_diagnostic_is_not_permission_and_counts_full_capture():
    values = [TARGET] * 10000
    with pytest.raises(PatchFactsError) as error:
        migration_state(values, TARGET)
    reason = str(error.value)
    assert reason.startswith("fact_migration_cardinality_unknown;diagnostic=")
    assert len(reason.encode()) <= 512
    diagnostic = json.loads(reason.split(";diagnostic=", 1)[1])
    assert diagnostic["rows"] == diagnostic["same"] == diagnostic["fqcn"] == 10000
    assert diagnostic["unknown"] == diagnostic["legacy"] == 0


def test_diagnostic_does_not_change_success_or_values():
    values = [TARGET, "20160101000000"]
    assert migration_state(values, TARGET) == ("executed", 1)
    assert values == [TARGET, "20160101000000"]
