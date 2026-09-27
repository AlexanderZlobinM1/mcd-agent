import json
import sys

from mcd_agent import __version__, cli


def test_nonroot_query_no_config_or_instance_read(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sys, "argv", ["mcd-cli", "mautic-target-patch-verification-capability", "--json"])
    monkeypatch.setattr("mcd_agent.config.load_config", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("config read")))
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"schema", "agent_version", "features", "contract_sha256", "command", "read_only",
        "original_plan_database_facts_supported", "online_admission_required", "purpose", "dispatch_authority"}
    assert result["agent_version"] == __version__
    assert result["features"] == {"target_patch_verification_v1": True}
    assert result["dispatch_authority"] is False
    assert result["original_plan_database_facts_supported"] is False
