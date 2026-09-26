import json
from pathlib import Path
import shutil
import pytest
from mcd_agent.mautic_patch_stage import TargetStage
from mcd_agent.mautic_patch_plan_v3 import PatchPlanV3Error


def scenario(tmp_path):
    fixture = Path(__file__).parents[1] / 'mcd_agent/contracts/fixtures/mautic-patch-resolution-v1.json'
    plan = json.loads(fixture.read_text())['resolve_response']['plan']
    plan.update(source_version='7.1.3', target_version='7.2.0', trigger='upgrade_lifecycle', phase='dependency_update_preflight')
    plan['patches'][0].update(triggers=['upgrade_lifecycle'], phases=['post_source_install'])
    root = tmp_path / 'live'
    path = root / 'docroot/app/fixture.txt'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'before\n')
    (root / 'version.txt').write_text('7.1.3')
    def prepare(stage):
        (stage / 'version.txt').write_text('7.2.0')
    def version(stage):
        return (stage / 'version.txt').read_text()
    return root, path, plan, prepare, version


def test_exact_target_is_validated_before_any_live_mutation_and_rechecked(tmp_path):
    root, path, plan, prepare, version = scenario(tmp_path)
    stage = TargetStage(str(root), plan, prepare, version)
    try:
        assert path.read_bytes() == b'before\n'
        assert version(root) == '7.1.3'
        assert stage.outcomes[0]['decision'] == 'candidate'
        stage.verify_original(str(root), plan, version)
        shutil.copytree(stage.root, root, dirs_exist_ok=True)
        evidence = stage.verify_live(str(root), plan, version)
        assert evidence['target_version'] == '7.2.0'
        assert len(evidence['target_source_sha256']) == 64
        path.write_bytes(b'operator drift\n')
        with pytest.raises(PatchPlanV3Error, match='target_live_source_hash_mismatch'):
            stage.verify_live(str(root), plan, version)
        assert path.read_bytes() == b'operator drift\n'
    finally:
        directory = stage.directory
        stage.close()
    assert not directory.exists()


def test_bad_target_gate_never_changes_original_source(tmp_path):
    root, path, plan, prepare, version = scenario(tmp_path)
    def incompatible(stage):
        prepare(stage)
        (stage / 'docroot/app/fixture.txt').write_bytes(b'unknown target\n')
    with pytest.raises(PatchPlanV3Error, match='ambiguous'):
        TargetStage(str(root), plan, incompatible, version)
    assert path.read_bytes() == b'before\n'
    assert version(root) == '7.1.3'


def test_source_change_during_admission_and_plan_change_are_rejected(tmp_path):
    root, path, plan, prepare, version = scenario(tmp_path)
    stage = TargetStage(str(root), plan, prepare, version)
    try:
        path.write_bytes(b'operator edit\n')
        with pytest.raises(PatchPlanV3Error, match='source_changed_before_mutation'):
            stage.verify_original(str(root), plan, version)
        changed = dict(plan, run_id='different')
        with pytest.raises(PatchPlanV3Error, match='source_binding_changed'):
            stage.verify_original(str(root), changed, version)
    finally:
        stage.close()


def test_wrong_target_version_and_external_symlink_fail_closed(tmp_path):
    root, path, plan, prepare, version = scenario(tmp_path)
    with pytest.raises(PatchPlanV3Error, match='target_stage_version_mismatch'):
        TargetStage(str(root), plan, lambda _: None, version)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'untouched')
    (root / 'other-link').symlink_to(outside)
    with pytest.raises(PatchPlanV3Error, match='external_symlink'):
        TargetStage(str(root), plan, prepare, version)
    assert outside.read_bytes() == b'untouched'
