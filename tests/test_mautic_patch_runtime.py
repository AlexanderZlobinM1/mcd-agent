import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mcd_agent import mautic_patch_runtime as runtime


def scenario(tmp_path):
    fixture = Path(__file__).parents[1] / 'mcd_agent/contracts/fixtures/mautic-patch-resolution-v1.json'
    resolved = json.loads(fixture.read_text())['resolve_response']
    source = tmp_path / 'docroot/app/fixture.txt'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'before\n')
    config = SimpleNamespace(mcc_url='https://fixture.example', mcc_token='fixture', php_bin='php')
    install = SimpleNamespace(root=str(tmp_path), instance_uid='fixture-instance-001')
    return config, install, source, resolved


def test_runtime_routes_authenticated_selected_plan_and_reports_immutable_evidence(tmp_path):
    config, install, source, resolved = scenario(tmp_path)
    with patch.object(runtime, 'resolve_plan', return_value=resolved) as resolve, \
         patch.object(runtime, 'report_evidence', return_value={'status':'accepted'}) as report, \
         patch.object(runtime, 'read_mautic_version_evidence_read_only', return_value={'version':'7.2.0'}), \
         patch.object(runtime, 'confirmed_mautic_major', return_value=7), \
         patch.object(runtime, '_host_identity', return_value=('host','fixture.example')), \
         patch('mcd_agent.mautic_patch_stage.application_root', return_value=tmp_path):
        result = runtime.run(config, install, phase='before_plugin_reload')
    assert result['status']=='success'
    assert source.read_bytes()==b'after\n'
    assert resolve.call_args.kwargs['trigger']=='daemon_reconcile'
    assert report.call_args.kwargs['plan_sha256']==resolved['plan_sha256']
    assert report.call_args.kwargs['evidence']['records'][0]['decision']=='applied'
    assert report.call_args.kwargs['evidence']['run_id']==resolved['plan']['run_id']


def test_blocked_catalog_never_uses_legacy_payload(tmp_path):
    config, install, source, resolved = scenario(tmp_path)
    with patch.object(runtime, 'resolve_plan', return_value={'status':'blocked','reason':'required_predicate_missing'}), \
         patch.object(runtime, 'read_mautic_version_evidence_read_only', return_value={'version':'7.2.0'}), \
         patch.object(runtime, 'confirmed_mautic_major', return_value=7):
        result = runtime.run(config, install, phase='before_plugin_reload')
    assert result['status']=='error'
    assert result['reason']=='required_predicate_missing'
    assert source.read_bytes()==b'before\n'
    assert not (tmp_path/'.mcd').exists()


def test_required_off_and_unknown_opt_in_remain_effective_before_catalog_selection(tmp_path):
    config, install, source, resolved = scenario(tmp_path)
    config.mautic6_core_patch_policy='off'
    with patch.object(runtime,'confirmed_mautic_major',return_value=6), \
         patch.object(runtime,'read_mautic_version_evidence_read_only',return_value={'version':'6.0.9'}), \
         patch.object(runtime,'resolve_plan') as resolve:
        assert runtime.run(config,install,phase='before_plugin_reload')['reason']=='policy_off'
    resolve.assert_not_called()
    config.mautic6_core_patch_policy='required'
    with patch.object(runtime,'confirmed_mautic_major',return_value=6), \
         patch.object(runtime,'read_mautic_version_evidence_read_only',return_value={'version':None}), \
         patch.object(runtime,'resolve_plan') as resolve:
        assert runtime.run(config,install,phase='before_plugin_reload')['reason']=='unknown_version_policy_off'
    resolve.assert_not_called()
    assert source.read_bytes()==b'before\n'
