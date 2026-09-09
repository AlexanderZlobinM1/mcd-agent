import json
import pytest
from mcd_agent import mautic_patch_plan as patch

def plan(kind="zip"):
    return json.dumps({"schema": patch.PLAN_SCHEMA, "registry_revision": patch.REGISTRY_REVISION, "source_version": "7.1.3", "target_version": "7.2.0", "install_type": kind, "patches": [{"id": patch.ROLE, "phase_order": 10, "phases": ["post_source_install", "before_doctrine_migrations"], "depends_on": [], "conflicts_with": []}, {"id": patch.ASSET, "phase_order": 20, "phases": ["post_source_install_before_asset_generation"], "depends_on": [patch.ROLE], "conflicts_with": []}]})

def seed(root, composer=False):
    source = root / "docroot" if composer else root
    if composer: (root / "composer.json").write_text("{}")
    (source / "app/migrations").mkdir(parents=True); (source / "app/bundles/CoreBundle").mkdir(parents=True)
    (source / "composer.lock").write_text('"version":"7.2.0"'); (source / patch._ROLE_PATH).write_text(patch._ROLE_OLD); (source / patch._BUNDLE_PATH).write_text(patch._BUNDLE_OLD)

@pytest.fixture(autouse=True)
def lint(monkeypatch): monkeypatch.setattr(patch, "_lint", lambda _: None)

@pytest.mark.parametrize("composer", [False, True])
def test_clean_713_to_720_phase_plan_is_idempotent(tmp_path, composer):
    seed(tmp_path, composer); raw = plan("composer" if composer else "zip")
    assert patch.execute(str(tmp_path), raw, "post_source_install", "upgrade-72")["status"] == "success"
    assert patch.execute(str(tmp_path), raw, "post_source_install_before_asset_generation", "upgrade-72")["status"] == "success"
    again = patch.execute(str(tmp_path), raw, "post_source_install_before_asset_generation", "upgrade-72")
    assert again["patches"][0]["state"] == "already"

def test_rejects_unknown_id_and_ambiguous_signature(tmp_path):
    seed(tmp_path); payload = json.loads(plan()); payload["patches"][0]["id"] = "arbitrary"
    with pytest.raises(patch.PatchPlanError): patch.execute(str(tmp_path), json.dumps(payload), "post_source_install", "run", "verify")
    (tmp_path / patch._ROLE_PATH).write_text(patch._ROLE_OLD * 2)
    assert patch.execute(str(tmp_path), plan(), "post_source_install", "run", "verify")["status"] == "error"

def test_rollback_requires_recorded_patched_checksum(tmp_path):
    seed(tmp_path); raw = plan(); patch.execute(str(tmp_path), raw, "post_source_install", "run")
    assert patch.rollback(str(tmp_path), raw, "run")["status"] == "success"
    assert (tmp_path / patch._ROLE_PATH).read_text() == patch._ROLE_OLD
