import json
from pathlib import Path
import pytest
from mcd_agent import mautic_patch_plan as patch
from mcd_agent import __version__


def plan():
    fixture = Path(__file__).parents[1] / 'mcd_agent/contracts/fixtures/mautic-patch-resolution-v1.json'
    return json.loads(fixture.read_text())['resolve_response']['plan']


def test_contract_has_no_embedded_patch_catalog_and_advertises_both_generic_versions():
    advertised = patch.contract()
    assert advertised['plan_schema'] == 'mcd-mautic-patch-plan-v3'
    assert 'patches' not in advertised
    assert 'mautic-patch-plan-v3' in advertised['capabilities']
    assert 'mautic-patch-plan-v2' in advertised['capabilities']
    assert advertised['minimum_agent_version'] == __version__


def test_static_catalog_is_rejected_without_source_access(tmp_path):
    with pytest.raises(patch.PatchPlanError, match='static_or_unknown'):
        patch.execute(str(tmp_path), '{"schema":"mcd-mautic-patch-plan-v1"}', 'post_source_install', 'run')
    assert list(tmp_path.iterdir()) == []


def test_v3_adapter_preserves_read_only_preflight_and_plan_binding(tmp_path):
    value = plan()
    source = tmp_path / 'docroot/app/fixture.txt'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'before\n')
    raw = json.dumps(value)
    patch.atomic_preflight(str(tmp_path), raw, value['run_id'])
    assert source.read_bytes() == b'before\n'
    patch.execute(str(tmp_path), raw, value['phase'], value['run_id'])
    assert source.read_bytes() == b'after\n'
    patch.rollback(str(tmp_path), json.dumps(dict(value, operation='rollback')), value['run_id'])
    assert source.read_bytes() == b'before\n'


def test_run_argument_mismatch_rejected_before_mutation(tmp_path):
    value = plan()
    with pytest.raises(patch.PatchPlanError, match='run_id_argument_mismatch'):
        patch.atomic_preflight(str(tmp_path), json.dumps(value), 'different')
    assert list(tmp_path.iterdir()) == []


def test_rejection_evidence_remains_terminal_and_reports_no_mutation():
    evidence = patch.rejected_preflight('run', 'invalid')
    assert evidence['status'] == 'error'
    assert evidence['upgrade_started'] is False
    assert evidence['rollback_attempted'] is False
    assert evidence['selected'] == evidence['applied'] == []
