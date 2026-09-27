import json
from types import SimpleNamespace

import pytest

from mcd_agent import mautic_upgrade as upgrade


@pytest.fixture
def observations(monkeypatch):
    instance = SimpleNamespace(root="/fixture/project", console_path="/fixture/project/bin/console",
                               instance_uid="fixture", local_php_path=None)
    monkeypatch.setattr(upgrade, "_pick_install_record", lambda *a: instance)
    monkeypatch.setattr(upgrade, "read_mautic_version_evidence_read_only",
                        lambda *a: {"version": "6.0.9", "source": "static_metadata"})
    monkeypatch.setattr(upgrade, "inspect_json_schema_repair", lambda **kw: {"status": "unsupported"})
    calls = []
    def composer(**kwargs):
        calls.append(kwargs)
        return {"status": "reused", "compatible": True,
                "php": {"available": True, "version": "PHP 8.3.33", "path": "/usr/bin/php"}}
    monkeypatch.setattr(upgrade, "composer_readiness", composer)
    return SimpleNamespace(php_bin="/usr/bin/php"), calls


@pytest.mark.parametrize("target", ["7.1.3", "7.2.1", "8.0.0"])
def test_preflight_uses_existing_observations_not_major_runtime_policy(observations, capsys, target):
    config, calls = observations
    assert upgrade.run_upgrade_preflight(config=config, root=None, mode="composer", target_override=target) == 0
    evidence = json.loads(capsys.readouterr().out.strip().split("=", 1)[1])
    assert evidence["target_version"] == target
    php = evidence["composer"]["php"]
    assert php["version"] == "PHP 8.3.33"
    assert not {"required_version", "decision", "policy", "compatible"}.intersection(php)
    assert calls == [{"php_bin": "/usr/bin/php", "allow_bootstrap": False}]


def test_composer_prepare_restores_native_bootstrap_without_system_upgrade(observations, capsys):
    config, calls = observations
    assert upgrade.run_upgrade_composer_prepare(config=config, root=None, mode="composer", target_override="7.1.3") == 0
    assert calls == [{"php_bin": "/usr/bin/php", "allow_bootstrap": True}]


@pytest.mark.parametrize("status", ["missing", "incompatible", "bootstrap_failure"])
def test_native_composer_failure_is_not_replaced_with_success(observations, monkeypatch, capsys, status):
    config, calls = observations
    monkeypatch.setattr(upgrade, "composer_readiness", lambda **kw: {"status": status, "compatible": False, "php": {"available": False}})
    assert upgrade.run_upgrade_preflight(config=config, root=None, mode="composer", target_override="7.1.3") == 1
    evidence = json.loads(capsys.readouterr().out.strip().split("=", 1)[1])
    assert evidence["status"] == "needs_attention"
    assert evidence["composer"]["status"] == status


def test_unverified_static_version_still_prevents_bootstrap(observations, monkeypatch):
    config, calls = observations
    monkeypatch.setattr(upgrade, "read_mautic_version_evidence_read_only",
                        lambda *a: {"version": "6.0.9", "source": "cache_fallback"})
    assert upgrade.run_upgrade_composer_prepare(config=config, root=None, mode="composer", target_override="7.1.3") == 1
    assert calls == [{"php_bin": "/usr/bin/php", "allow_bootstrap": False}]
