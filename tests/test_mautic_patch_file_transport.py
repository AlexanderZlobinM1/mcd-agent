import json
from pathlib import Path
import pytest
from mcd_agent.mautic_patch_resolution import read_plan_file, canonical_json_sha256, MauticPatchResolutionError


def test_private_file_accepts_original_canonical_hash_despite_formatting(tmp_path):
    value = {'run_id':'immutable-run','schema':'fixture','payload':'large bounded input'}
    file = tmp_path / 'plan.json'
    file.write_text(json.dumps(value, indent=2))
    file.chmod(0o600)
    assert json.loads(read_plan_file(str(file), canonical_json_sha256(value))) == value


def test_bad_hash_permissions_symlink_duplicate_key_are_rejected(tmp_path):
    value = {'run_id':'immutable-run'}
    file = tmp_path / 'plan.json'
    file.write_text(json.dumps(value))
    file.chmod(0o600)
    with pytest.raises(MauticPatchResolutionError, match='sha256_mismatch'):
        read_plan_file(str(file), '0'*64)
    file.chmod(0o644)
    with pytest.raises(MauticPatchResolutionError, match='permissions'):
        read_plan_file(str(file), canonical_json_sha256(value))
    file.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(file)
    with pytest.raises(OSError):
        read_plan_file(str(link), canonical_json_sha256(value))
    file.write_text('{"run_id":"first","run_id":"second"}')
    with pytest.raises(MauticPatchResolutionError, match='duplicate_key'):
        read_plan_file(str(file), canonical_json_sha256({'run_id':'second'}))


def test_file_transport_requires_absolute_path_and_digest(tmp_path):
    with pytest.raises(MauticPatchResolutionError, match='sha256_required'):
        read_plan_file(str(tmp_path/'absent'), '')
    with pytest.raises(MauticPatchResolutionError, match='absolute_path'):
        read_plan_file('relative.json', '0'*64)
