import json
import sys

from mcd_agent import cli


def test_cli_nonroot_is_single_bounded_json_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    argv = ["mcd-cli", "mautic-target-patch-verify", "--root", "/var/www/fixture",
            "--prepared-target-id", "p" * 32, "--catalog-file", "/missing/catalog",
            "--catalog-sha256", "a" * 64, "--json"]
    for name in ("verification-plan", "authorization-context", "patch-plan"):
        argv += ["--" + name + "-file", "/missing/" + name, "--" + name + "-sha256", "a" * 64]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out) == dict(
        schema="mcd-mautic-target-patch-verification-error-v1", status="rejected", code="input_invalid")
