from types import SimpleNamespace
import pytest
from mcd_agent.mautic_patch_fact_binding import _select_instance, validate_context
from mcd_agent.mautic_patch_facts import PatchFactsError


def layout(path):
    for name in ("app", "plugins", "bin"):
        (path / name).mkdir(parents=True, exist_ok=True)
    (path / "bin/console").write_text("fixture")
    return path


@pytest.mark.parametrize("relative", ["", "docroot", "public"])
def test_selected_root_ignores_unrelated_legacy_missing_and_malformed_layouts(tmp_path, relative):
    project = tmp_path / "selected"
    app = layout(project / relative)
    sibling = tmp_path / "other"
    (sibling / "app").mkdir(parents=True)
    legacy = tmp_path / "legacy"
    (legacy / "app").mkdir(parents=True)
    selected = SimpleNamespace(root=str(project), instance_uid="selected-uid")
    foreign = [SimpleNamespace(root=str(path), instance_uid="foreign-uid") for path in
               (sibling, legacy, tmp_path / "missing", layout(tmp_path / "valid-other"))]
    actual, root = _select_instance([*foreign, selected], str(app))
    assert actual is selected
    assert root == app


def test_foreign_root_is_never_selected_even_with_matching_uid(tmp_path):
    selected = layout(tmp_path / "selected")
    other = layout(tmp_path / "other")
    with pytest.raises(PatchFactsError, match="ambiguous"):
        _select_instance([SimpleNamespace(root=str(other), instance_uid="same-uid")], str(selected))


def test_two_owner_roots_for_one_application_are_ambiguous(tmp_path):
    project = tmp_path / "project"
    app = layout(project / "docroot")
    with pytest.raises(PatchFactsError, match="ambiguous"):
        _select_instance([SimpleNamespace(root=str(project)), SimpleNamespace(root=str(app))], str(app))


def test_malformed_records_fail_closed_with_stable_error():
    with pytest.raises(PatchFactsError, match="records_invalid"):
        validate_context({"patches": ["not-a-record"]})
