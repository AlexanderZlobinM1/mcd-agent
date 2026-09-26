from types import SimpleNamespace
import pytest
from mcd_agent.mautic_patch_fact_binding import _select_instance, validate_context, discover_execution_context
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


@pytest.mark.parametrize("prefix", ["", "ss_"])
def test_resolve_context_requires_explicit_discovered_prefix(tmp_path, monkeypatch, prefix):
    root = layout(tmp_path / "selected")
    db = SimpleNamespace(table_prefix=prefix, name="fixture_db")
    install = SimpleNamespace(root=str(root), instance_uid="uid", db=db, local_php_path="fixture.php")
    config = SimpleNamespace(discovery_roots=[], exclude_path_contains=[], supported_mautic_majors=[], custom_instances=[])
    monkeypatch.setattr("mcd_agent.discovery.discover_mautic", lambda *args: [install])
    monkeypatch.setattr("mcd_agent.localphp.parse_local_php", lambda path: {"db_table_prefix": prefix, "db_name": "fixture_db"})
    assert discover_execution_context(config, install) == {
        "instance_uid": "uid", "application_root": str(root), "table_prefix": prefix}
    monkeypatch.setattr("mcd_agent.localphp.parse_local_php", lambda path: {"db_name": "fixture_db"})
    assert discover_execution_context(config, install) is None


def test_resolve_context_never_invents_legacy_identity():
    assert discover_execution_context(SimpleNamespace(), SimpleNamespace(instance_uid="uid")) is None
