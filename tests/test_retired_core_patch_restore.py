from types import SimpleNamespace
from mcd_agent.mautic_core_restore import restore_retired_mcd_core_patches


def test_retired_restoration_does_not_guess_legacy_backup_or_overwrite_drift(tmp_path):
    source = tmp_path / 'source.php'
    backup = tmp_path / 'source.php.mcd-bak'
    source.write_bytes(b'operator drift')
    backup.write_bytes(b'old backup')
    result = restore_retired_mcd_core_patches(SimpleNamespace(root=str(tmp_path)))
    assert result['reason'] == 'legacy_restore_requires_catalog_descriptor'
    assert source.read_bytes() == b'operator drift'
    assert backup.read_bytes() == b'old backup'
