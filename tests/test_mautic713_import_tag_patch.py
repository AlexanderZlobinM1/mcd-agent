from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mcd_agent.mautic713_import_tag_patch import ensure_patch, patch_status, revert_patch, reconcile_import_tag_patch


def test_missing_catalog_has_no_legacy_payload_fallback(tmp_path):
    source = tmp_path / 'Import.php'
    source.write_bytes(b'operator source')
    install = SimpleNamespace(root=str(tmp_path))
    assert ensure_patch(install)['reason'] == 'catalog_resolution_required'
    assert patch_status(install)['reason'] == 'catalog_resolution_required'
    assert source.read_bytes() == b'operator source'


def test_operator_rollback_requires_original_run_and_uses_catalog_descriptor(tmp_path):
    install = SimpleNamespace(root=str(tmp_path))
    config = SimpleNamespace(mcc_url='https://fixture.example')
    assert revert_patch(install, config)['reason'] == 'rollback_run_id_required'
    with patch('mcd_agent.mautic713_import_tag_patch.run', return_value={'status': 'success'}) as run:
        revert_patch(install, config, run_id='original-run')
    run.assert_called_once_with(config, install, phase='before_background_imports', operation='rollback', trigger='operator_action', run_id='original-run')


def test_daemon_pause_prevents_resolver_and_mutation(tmp_path):
    pause = tmp_path / 'pause'
    pause.touch()
    config = SimpleNamespace(scheduler_pause_flag_path=str(pause), mcc_url='https://fixture.example')
    with patch('mcd_agent.mautic_patch_runtime.run') as run:
        assert reconcile_import_tag_patch(config, [SimpleNamespace(root=str(tmp_path))]) == []
    run.assert_not_called()
