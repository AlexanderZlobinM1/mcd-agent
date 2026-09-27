import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcd_agent import composer_permissions as permissions


def project(root, installed=True):
    root.mkdir(exist_ok=True)
    (root / "composer.json").write_text(json.dumps({"extra": {"installer-paths": {"docroot/plugins/{$name}": ["type:plugin"]}}}))
    (root / "composer.lock").write_text('{"packages":[]}')
    vendor = root / "vendor/composer"
    vendor.mkdir(parents=True)
    if installed:
        (vendor / "installed.json").write_text(json.dumps({"packages": [
            {"name": "fixture/plugin", "install-path": "../../docroot/plugins/FixtureBundle"}]}))
    dependency = root / "docroot/plugins/FixtureBundle/Assets/library/js/deep/below/four/dist"
    dependency.mkdir(parents=True)
    (dependency / "source.js").write_bytes(b"unchanged source\n")
    return dependency


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setattr(permissions.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))


def test_nested_directory_mode_prepared_without_source_change(tmp_path):
    directory = project(tmp_path / "project")
    source = directory / "source.js"; original = source.stat()
    directory.chmod(0o555)
    result = permissions.prepare_composer_paths(str(tmp_path / "project"))
    assert result["repaired_directories"] == 1
    assert directory.stat().st_mode & 0o700 == 0o700
    assert source.read_bytes() == b"unchanged source\n"
    assert source.stat().st_ino == original.st_ino and source.stat().st_mode == original.st_mode
    assert permissions.prepare_composer_paths(str(tmp_path / "project"))["repaired_directories"] == 0


def test_foreign_parent_owner_changes_only_that_directory(tmp_path, monkeypatch):
    directory = project(tmp_path / "project")
    original_stat = os.stat; changes = []
    def stat_result(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if Path(path) == directory:
            values = list(value); values[4] = os.getuid() + 1
            return os.stat_result(values)
        return value
    monkeypatch.setattr(os, "stat", stat_result)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid, **kw: changes.append((Path(path), uid, gid)))
    result = permissions.prepare_composer_paths(str(tmp_path / "project"))
    assert result["repaired_directories"] == 1
    assert changes == [(directory, os.getuid(), os.getgid())]
    assert (directory / "source.js").read_bytes() == b"unchanged source\n"


def test_staged_custom_vendor_destination_maps_to_live_project(tmp_path):
    live = tmp_path / "live"; staged = tmp_path / "staged"
    project(live); project(staged)
    manifest = {"config": {"vendor-dir": "dependencies"}}
    (staged / "composer.json").write_text(json.dumps(manifest))
    metadata = staged / "dependencies/composer"; metadata.mkdir(parents=True)
    (metadata / "installed.json").write_text(json.dumps({"packages": [
        {"install-path": "../../custom/package"}]}))
    destination = live / "custom/package"; destination.mkdir(parents=True); destination.chmod(0o555)
    assert permissions.prepare_composer_paths(str(live), target_project_root=str(staged))["repaired_directories"] == 1
    assert destination.stat().st_mode & 0o700 == 0o700


def test_symlink_escape_is_not_modified(tmp_path):
    live = tmp_path / "live"; outside = tmp_path / "outside"; outside.mkdir()
    live.mkdir(); (live / "composer.json").write_text('{"config":{"vendor-dir":"vendor"}}')
    (live / "vendor").symlink_to(outside, target_is_directory=True)
    before = outside.stat()
    with pytest.raises(RuntimeError, match="escapes"):
        permissions.prepare_composer_paths(str(live))
    assert outside.stat().st_mode == before.st_mode and outside.stat().st_uid == before.st_uid


def test_native_permission_failure_propagates_without_source_write(tmp_path, monkeypatch):
    directory = project(tmp_path / "project"); directory.chmod(0o555)
    def fail(*args, **kwargs): raise PermissionError("filesystem refused normal preparation")
    monkeypatch.setattr(os, "chmod", fail)
    with pytest.raises(PermissionError): permissions.prepare_composer_paths(str(tmp_path / "project"))
    assert (directory / "source.js").read_bytes() == b"unchanged source\n"
