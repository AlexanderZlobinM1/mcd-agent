from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from mcd_agent import mautic_manual_upgrade as manual
from mcd_agent import mautic_upgrade as upgrade


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    root = (tmp_path / "project")
    root.mkdir()
    root = root.resolve()
    metadata = root / "docroot/app/bundles/CoreBundle/release_metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"version":"7.1.3"}')
    (root / "composer.json").write_text('{"name":"mautic/recommended-project","require":{"mautic/core-lib":"7.1.3"}}')
    (root / "composer.lock").write_text('{"packages":[{"name":"mautic/core-lib","version":"7.1.3"}]}')
    fixture = Path(__file__).parent / "fixtures/mautic_patch_plan/mcc-e74cdc2b-plan.json"
    monkeypatch.setattr(manual.os, "geteuid", lambda: 0)
    return dict(root=str(root), install_root=str(root), current="7.1.3", target="7.2.0",
                mode="composer", raw_plan=fixture.read_text(), run_id="manual-job-72",
                yes=True, allow_minor=True, allow_major=False, with_system_upgrade=False)


def test_exact_manual_invocation_is_valid(invocation):
    manual.validate_preflighted_single_instance(**invocation)


@pytest.mark.parametrize("key,value", [
    ("root", None), ("root", "project"), ("root", "/"),
    ("current", "7.1.2"), ("current", "7.2.0"), ("target", None),
    ("target", "7.2.1"), ("target", "8.0.0"), ("mode", "auto"),
    ("mode", "zip"), ("raw_plan", None), ("raw_plan", "{}"),
    ("raw_plan", " " * 16385), ("run_id", None), ("run_id", ""),
    ("run_id", "../other"), ("run_id", "x" * 97), ("yes", False),
    ("allow_minor", False), ("allow_major", True), ("with_system_upgrade", True),
])
def test_manual_input_mismatch_rejected(invocation, key, value):
    invocation[key] = value
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**invocation)


def test_non_root_rejected(invocation, monkeypatch):
    monkeypatch.setattr(manual.os, "geteuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="root execution"):
        manual.validate_preflighted_single_instance(**invocation)


def test_noncanonical_alias_and_wrong_instance_rejected(invocation, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(invocation["root"], target_is_directory=True)
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**{**invocation, "root": str(alias)})
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**{**invocation, "install_root": str(tmp_path)})


@pytest.mark.parametrize("mutation", ["duplicate", "float", "unknown", "revision", "source", "mode", "order"])
def test_strict_patch_plan_rejected(invocation, mutation):
    plan = json.loads(invocation["raw_plan"])
    if mutation == "duplicate":
        invocation["raw_plan"] = '{"schema":"mcd-mautic-patch-plan-v1",' + invocation["raw_plan"].lstrip()[1:]
    else:
        if mutation == "float":
            plan["patches"][0]["phase_order"] = 10.0
        elif mutation == "unknown":
            plan["commands"] = []
        elif mutation == "revision":
            plan["registry_revision"] = "unknown"
        elif mutation == "source":
            plan["source_version"] = "7.1.2"
        elif mutation == "mode":
            plan["install_type"] = "zip"
        else:
            plan["patches"].reverse()
        invocation["raw_plan"] = json.dumps(plan)
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**invocation)


@pytest.mark.parametrize("missing", [False, True])
def test_source_metadata_ambiguity_or_absence_rejected(invocation, missing):
    root = Path(invocation["root"])
    metadata = root / "docroot/app/bundles/CoreBundle/release_metadata.json"
    if missing:
        metadata.unlink()
        (root / "composer.lock").unlink()
    else:
        metadata.write_text('{"version":"7.1.2"}')
    with pytest.raises(RuntimeError):
        manual.validate_preflighted_single_instance(**invocation)


@dataclass
class Install:
    root: str
    console_path: str = "bin/console"
    runtime: str = "host"
    mautic_major: int = 7


def wire_upgrade(invocation, monkeypatch, kind="composer"):
    root = Path(invocation["root"])
    if kind == "zip":
        (root / "composer.json").write_text('{"name":"mautic/core","require":{}}')
        (root / "docroot/app").rename(root / "app")
        (root / "docroot").rmdir()
        plan = json.loads(invocation["raw_plan"])
        plan["install_type"] = "zip"
        invocation.update(mode="zip", raw_plan=json.dumps(plan))
    events = []
    installed = [False]
    cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data",
                          mcc_url="https://mcc.example.test", mcc_token="test-shared-token")
    monkeypatch.setattr(upgrade, "_pick_install_record", lambda *a: Install(str(root)))
    monkeypatch.setattr(upgrade, "_read_current_version", lambda *a: "7.2.0" if installed[0] else "7.1.3")

    def deny(request, **kwargs):
        events.append("global_authorization")
        raise HTTPError(request.full_url, 409, "release blocked", {}, None)

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", deny)
    monkeypatch.setattr(upgrade, "_enter_upgrade_maintenance", lambda *a: events.append("maintenance") or object())
    monkeypatch.setattr(upgrade, "_exit_upgrade_maintenance", lambda *a: events.append("cleanup"))
    monkeypatch.setattr(upgrade, "_pre_upgrade_permissions_check", lambda *a: events.append("permissions"))
    monkeypatch.setattr(upgrade, "revert_mautic713_import_tag_patch", lambda *a: events.append("revert") or {"status": "clean"})

    def install(*args):
        events.append("install")
        installed[0] = True

    monkeypatch.setattr(upgrade, "_apply_zip", install)
    monkeypatch.setattr(upgrade, "_apply_composer", install)
    for name in ("ensure_mailer_packages_for_sender_config", "ensure_amazon_mailer_for_bundles", "_post_upgrade_verify"):
        monkeypatch.setattr(upgrade, name, lambda *a, **kw: None)
    monkeypatch.setattr(upgrade, "installed_required_bundles", lambda *a: [])
    monkeypatch.setattr(upgrade, "ensure_import_tag_patch", lambda *a: {"status": "already"})
    monkeypatch.setattr(upgrade, "_write_upgrade_version_cache", lambda *a: 0)
    args = dict(config=cfg, root=str(root), mode=kind, yes=True, do_backup=False,
                with_system_upgrade=False, target_override="7.2.0", allow_minor=True,
                patch_plan_json=invocation["raw_plan"], patch_run_id=invocation["run_id"])
    return args, events


@pytest.mark.parametrize("kind", ["zip", "composer"])
def test_explicit_manual_flow_skips_both_callbacks(invocation, monkeypatch, kind):
    args, events = wire_upgrade(invocation, monkeypatch, kind)
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    assert events == ["maintenance", "permissions", "revert", "install", "cleanup"]


@pytest.mark.parametrize("kind", ["zip", "composer"])
def test_ordinary_blocked_release_never_mutates_source(invocation, monkeypatch, kind):
    args, events = wire_upgrade(invocation, monkeypatch, kind)
    with pytest.raises(RuntimeError, match="not authorized"):
        upgrade.run_upgrade_apply(**args)
    assert events == ["global_authorization"]


def test_invalid_manual_plan_rejected_before_maintenance(invocation, monkeypatch):
    args, events = wire_upgrade(invocation, monkeypatch)
    args["patch_plan_json"] = "{}"
    with pytest.raises(RuntimeError):
        upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True)
    assert events == []


def test_source_change_during_maintenance_aborts_before_first_mutation(invocation, monkeypatch):
    args, events = wire_upgrade(invocation, monkeypatch)

    def changed(*a):
        events.append("maintenance")
        (Path(invocation["root"]) / "composer.lock").write_text('{"packages":[{"name":"mautic/core-lib","version":"7.1.2"}]}')
        return object()

    monkeypatch.setattr(upgrade, "_enter_upgrade_maintenance", changed)
    with pytest.raises(RuntimeError):
        upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True)
    assert events == ["maintenance", "cleanup"]


@pytest.mark.parametrize("operation", ["check", "interactive"])
def test_cli_cannot_ignore_manual_flag_on_other_operations(monkeypatch, operation):
    from mcd_agent import cli
    monkeypatch.setattr("sys.argv", ["mcd-cli", "mautic-upgrade", operation, "--mcc-preflighted-single-instance"])
    assert cli.main() == 2


def test_cli_forwards_manual_flag_and_exact_job_fields(invocation, monkeypatch):
    from mcd_agent import cli
    captured = {}
    monkeypatch.setattr(cli, "load_config", lambda *a: object())
    monkeypatch.setattr(cli, "maybe_notify_update", lambda *a: None)
    monkeypatch.setattr(cli, "run_upgrade_apply", lambda **kw: captured.update(kw) or 2)
    monkeypatch.setattr("sys.argv", ["mcd-cli", "mautic-upgrade", "apply", "--root", invocation["root"],
        "--mode", "composer", "--target", "7.2.0", "--allow-minor", "--yes",
        "--patch-plan-json", invocation["raw_plan"], "--patch-run-id", invocation["run_id"],
        "--mcc-preflighted-single-instance"])
    assert cli.main() == 2
    assert captured["mcc_preflighted_single_instance"] is True
    assert captured["root"] == invocation["root"]
    assert captured["patch_plan_json"] == invocation["raw_plan"]
    assert captured["patch_run_id"] == invocation["run_id"]
