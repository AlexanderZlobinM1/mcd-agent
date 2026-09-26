from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from mcd_agent import mautic_release_authorization as auth


def context():
    now = datetime.now(timezone.utc)
    value = {key: "fixture" for key in auth.BINDINGS}
    value.update(schema=auth.CONTEXT_SCHEMA, host_id="00000000-0000-0000-0000-000000000001",
                 application_root="/fixture/app", policy_sha256="a" * 64, plan_sha256="b" * 64,
                 signature="c" * 64, nonce="n" * 24, issued_at=now.isoformat(),
                 expires_at=(now + timedelta(minutes=10)).isoformat())
    value["transition_requirements"] = {
        "requires_json_repair": False, "requires_backup": False, "system_upgrade_supported": False,
        "requires_latest_source": True, "database_compatibility": "", "minimum_agent_version": "1.2.67",
        "install_types": ["composer"], "phases": ["upgrade"]}
    return value


def root_fixture(tmp_path, monkeypatch, raw):
    path = tmp_path / "authorization.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    original = auth.os.fstat
    def fstat(fd):
        item = original(fd)
        return SimpleNamespace(st_uid=0, st_mode=item.st_mode, st_size=item.st_size,
                               st_mtime_ns=item.st_mtime_ns, st_ctime_ns=item.st_ctime_ns)
    monkeypatch.setattr(auth.os, "fstat", fstat)
    return path


def test_private_canonical_file_hash_and_symlink(tmp_path, monkeypatch):
    value = context()
    raw = auth.canonical(value)
    path = root_fixture(tmp_path, monkeypatch, raw)
    sha = hashlib.sha256(raw).hexdigest()
    assert auth.read_context(str(path), sha) == value
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="unavailable"):
        auth.read_context(str(alias), sha)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        auth.read_context(str(path), sha)


@pytest.mark.parametrize("raw", [b'{"schema":1,"schema":2}', b'{"a":NaN}', b'{}\n', b'x' * 16385])
def test_invalid_private_input_fails_closed(tmp_path, monkeypatch, raw):
    path = root_fixture(tmp_path, monkeypatch, raw)
    with pytest.raises(ValueError):
        auth.read_context(str(path), hashlib.sha256(raw).hexdigest())


def test_binding_and_expiry_are_independent_gates():
    value = context()
    auth.validate_context(value, {"instance_uid": "fixture"})
    with pytest.raises(ValueError, match="binding"):
        auth.validate_context(value, {"instance_uid": "other"})
    value["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="expired"):
        auth.validate_context(value, {})


def test_live_post_exact_echo_no_signature_response(monkeypatch):
    value = context()
    sha = hashlib.sha256(auth.canonical(value)).hexdigest()
    echo = {key: value[key] for key in auth.BINDINGS}
    echo.update(schema=auth.SCHEMA, status="authorized")
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return auth.canonical(echo)
    def open_url(request, timeout):
        calls.append(request)
        return Response()
    monkeypatch.setattr(auth.urllib.request, "urlopen", open_url)
    auth.authorize(SimpleNamespace(mcc_url="https://mcc.example", mcc_token="private"), value, sha, {})
    assert json.loads(calls[0].data) == {"authorization_context": value, "authorization_context_sha256": sha}
    assert calls[0].get_method() == "POST"
    echo["patch_run_id"] = "wrong"
    with pytest.raises(RuntimeError, match="rejected"):
        auth.authorize(SimpleNamespace(mcc_url="https://mcc.example", mcc_token="private"), value, sha, {})


@pytest.mark.parametrize("runtime,kind,supported", [("host", "composer", True), ("host", "zip", True),
                                                    ("docker", "composer", False), ("host", "unknown", False)])
def test_capability_only_advertised_for_supported_executor(runtime, kind, supported):
    result = auth.runtime_capabilities(runtime, kind, ["existing", auth.SCHEMA])
    assert "existing" in result
    assert (auth.SCHEMA in result) is supported


@pytest.mark.parametrize("field,value", [("requires_backup", 1), ("database_compatibility", "unknown"),
                                         ("minimum_agent_version", "1.2.67-beta"), ("install_types", ["docker"]),
                                         ("phases", ["dependency_update_preflight"])])
def test_requirements_fail_closed_on_wrong_types_or_unknown_values(field, value):
    signed = context()
    signed["transition_requirements"][field] = value
    with pytest.raises(ValueError, match="requirements_invalid"):
        auth.validate_context(signed, {})
