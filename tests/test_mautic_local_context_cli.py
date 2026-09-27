import json
import sys

from test_mautic_patch_context import fixture
from mcd_agent import cli


def test_actual_cli_static_context_json(tmp_path, monkeypatch, capsys):
    config, roots = fixture(tmp_path, monkeypatch)
    root = roots[0]
    (root / "app/release_metadata.json").write_text('{"version":"7.2.0"}')
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["mcd-cli", "mautic-local-context", "--config", str(config),
                                     "--root", str(root), "--json"])
    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"schema", "context", "layout", "source_version", "version_evidence_source"}
    assert output["source_version"] == "7.2.0"
    assert output["layout"]["application_root_relative"] == "."


def test_nonroot_cli_error_no_context(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sys, "argv", ["mcd-cli", "mautic-local-context", "--config", "/missing",
                                     "--root", str(tmp_path), "--json"])
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out) == dict(schema="mcd-mautic-local-context-v2",
        status="error", code="local_context_selection_invalid")
