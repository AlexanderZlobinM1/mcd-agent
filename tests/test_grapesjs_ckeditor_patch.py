from types import SimpleNamespace
from unittest.mock import patch
from mcd_agent.grapesjs_ckeditor_patch import ensure_grapesjs_ckeditor_gpl_patch


def test_missing_catalog_does_not_change_source(tmp_path):
    source = tmp_path / 'builder.js'
    source.write_bytes(b'operator source')
    result = ensure_grapesjs_ckeditor_gpl_patch(SimpleNamespace(root=str(tmp_path)))
    assert result['reason'] == 'catalog_resolution_required'
    assert source.read_bytes() == b'operator source'


def test_asset_compatibility_name_routes_only_to_generic_phase():
    install, config = object(), object()
    with patch('mcd_agent.grapesjs_ckeditor_patch.run', return_value={'status': 'skip'}) as run:
        ensure_grapesjs_ckeditor_gpl_patch(install, config)
    run.assert_called_once_with(config, install, phase='before_asset_generation')
