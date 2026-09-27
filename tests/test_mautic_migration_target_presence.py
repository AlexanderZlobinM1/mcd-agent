import json

import pytest

from mcd_agent.mautic_patch_facts import PatchFactsError, digest, migration_state

TARGET = "Fixture\\Migrations\\Version20211209022550"
OTHER = "Other\\Migrations\\Version20160101000000"
LITERAL = OTHER.replace("\\", "\\\\")


@pytest.mark.parametrize("values", [[TARGET, LITERAL], [LITERAL, TARGET]])
def test_positive_raw_presence_leaves_unrelated_literals_uninterpreted(values):
    before = list(values); raw_hash = digest(sorted(values))
    assert migration_state(values, TARGET) == ("executed", 1)
    assert values == before and digest(sorted(values)) == raw_hash
    assert LITERAL in values and OTHER not in values


def test_absent_target_with_literal_is_unknown_not_pending():
    with pytest.raises(PatchFactsError) as error:
        migration_state([LITERAL], TARGET)
    diagnostic = json.loads(str(error.value).split(";diagnostic=", 1)[1])
    assert diagnostic["literal"] == 1 and diagnostic["exact"] == 0
    assert diagnostic["alias"] == diagnostic["unsafe"] == 0


@pytest.mark.parametrize("alias", [TARGET.replace("\\", "\\\\"),
    TARGET.lower().replace("\\", "\\\\"), "Different\\\\Namespace\\\\Version20211209022550",
    "Version20211209022550", "20211209022550"])
@pytest.mark.parametrize("present", [False, True])
def test_any_target_alias_blocks_even_raw_exact_presence(alias, present):
    with pytest.raises(PatchFactsError, match="alias_ambiguous"):
        migration_state(([TARGET] if present else []) + [alias], TARGET)


@pytest.mark.parametrize("rows", [[OTHER, LITERAL], [LITERAL, OTHER], [LITERAL, LITERAL],
    [OTHER.lower(), LITERAL]])
def test_raw_or_logical_duplicates_are_not_deduplicated(rows):
    with pytest.raises(PatchFactsError, match="ambiguous|cardinality_unknown") as error:
        migration_state([TARGET, *rows], TARGET)
    diagnostic = json.loads(str(error.value).split(";diagnostic=", 1)[1])
    assert diagnostic["logical_dup"] == 1


@pytest.mark.parametrize("invalid", [None, b"bytes", "", "\udcff", "BareUnrelatedClass",
    "\\" + LITERAL, LITERAL + "\\", "Other\\Migrations\\\\Version20160101000000",
    OTHER.replace("\\", "\\\\\\"), OTHER.replace("\\", "\\\\\\\\"),
    "Other\\\\Bad-Name\\\\Version20160101000000", "Other\\\\\\\\Migrations\\\\Version20160101000000"])
def test_unsafe_or_nonuniform_history_still_blocks_positive_target(invalid):
    with pytest.raises(PatchFactsError, match="encoding_unknown"):
        migration_state([TARGET, invalid], TARGET)


def test_relevance_counts_distinguish_category_count_from_duplicate_count():
    second = "More\\\\Migrations\\\\Version20170101000000"
    with pytest.raises(PatchFactsError) as error:
        migration_state([LITERAL, second], TARGET)
    diagnostic = json.loads(str(error.value).split(";diagnostic=", 1)[1])
    assert diagnostic["same"] == diagnostic["literal"] == 2
    assert diagnostic["raw_dup"] == diagnostic["logical_dup"] == diagnostic["alias"] == 0
    assert len(str(error.value).encode()) <= 512
