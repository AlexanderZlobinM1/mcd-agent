import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from mcd_agent import mautic_patch_plan as patch

FIXTURES = Path(__file__).parent / "fixtures" / "mautic_patch_plan"
ROLE_PATH = "app/migrations/Version20211209022550.php"
ORIGINAL_SHA = "f970321517fa32eed01a031f5110f397e441bb049965efdbbeece7750df4d33c"
FIXED_SHA = "b690b3cdd927a9b8257572cbb7bc42aba79f6c8b90d1ce39bac3154a928f2328"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan(kind="composer"):
    payload = json.loads((FIXTURES / "mcc-e74cdc2b-plan.json").read_text())
    payload["install_type"] = kind
    return json.dumps(payload, separators=(",", ":"))


def seed(root, kind="composer", version="7.2.0"):
    source = root / "docroot" if kind == "composer" else root
    (source / "app/migrations").mkdir(parents=True)
    bundle_dir = source / "app/bundles/CoreBundle"
    bundle_dir.mkdir(parents=True)
    (root / "composer.json").write_text(json.dumps({
        "name": "mautic/recommended-project" if kind == "composer" else "mautic/core",
        "require": {"mautic/core-lib": version} if kind == "composer" else {},
    }))
    (root / "composer.lock").write_text(json.dumps({
        "packages": [{"name": "mautic/core-lib", "version": version}],
    }))
    (bundle_dir / "release_metadata.json").write_text(json.dumps({"version": version}))
    shutil.copyfile(FIXTURES / "Version20211209022550-7.2.0.php", source / ROLE_PATH)
    shutil.copyfile(FIXTURES / "MauticCoreBundle-7.2.0.php", bundle_dir / "MauticCoreBundle.php")
    return source.resolve()


def test_real_fixture_reproduces_published_124_zero_counts(tmp_path):
    source = seed(tmp_path)
    role = source / ROLE_PATH
    assert sha(role) == ORIGINAL_SHA
    assert sha(source / "app/bundles/CoreBundle/MauticCoreBundle.php") == "8887573631466fec3a99329ba044f8f5fe8e093649685d3fe8961ce8e9cca651"
    text = role.read_text()
    assert text.count("foreach ($roles as $role) {\n    $rawPermissions = $role->getRawPermissions();") == 0
    assert text.count("$role = is_array($roleResult)") == 0
    gate = patch._gate(source, patch.ROLE)
    assert gate["state"] == "vulnerable"
    assert gate["files"][0]["vulnerable_count"] == 1
    assert gate["files"][0]["fixed_count"] == 0


@pytest.mark.parametrize("kind", ["zip", "composer"])
def test_real_source_phases_and_repeat_preserve_all_backups(tmp_path, kind):
    source = seed(tmp_path, kind)
    raw = plan(kind)
    before = (source / ROLE_PATH).read_bytes()
    initial = patch.execute(str(tmp_path), raw, "post_source_install", "upgrade-72", "verify")
    assert initial["status"] == "success"
    assert (source / ROLE_PATH).read_bytes() == before
    first = patch.execute(str(tmp_path), raw, "post_source_install", "upgrade-72")
    assert first["patches"][0]["state"] == "applied"
    assert first["patches"][0]["gate"]["files"][0]["sha256"] == ORIGINAL_SHA
    assert sha(source / ROLE_PATH) == FIXED_SHA
    second = patch.execute(str(tmp_path), raw, "before_doctrine_migrations", "upgrade-72")
    assert second["patches"][0]["state"] == "already"
    assert second["patches"][0]["gate"]["files"][0]["fixed_count"] == 1
    asset = patch.execute(str(tmp_path), raw, "post_source_install_before_asset_generation", "upgrade-72")
    assert asset["status"] == "success"
    assert asset["patches"][0]["state"] == "applied"
    repeated = patch.execute(str(tmp_path), raw, "post_source_install_before_asset_generation", "upgrade-72")
    assert repeated["patches"][0]["state"] == "already"
    evidence = json.loads((tmp_path / ".mcd/patch-runs/upgrade-72/result.json").read_text())
    assert len(evidence["backup_records"]) == 3
    assert patch.rollback(str(tmp_path), raw, "upgrade-72")["status"] == "success"
    assert (source / ROLE_PATH).read_bytes() == before
    assert not (source / patch._ASSET_PATH).exists()
    assert sha(source / patch._BUNDLE_PATH) == "8887573631466fec3a99329ba044f8f5fe8e093649685d3fe8961ce8e9cca651"


@pytest.mark.parametrize("mutation,old_count,fixed_count", [
    ("unknown", 0, 0), ("duplicate", 2, 0), ("mixed", 1, 1),
    ("changed", 1, 0), ("fixed_changed", 0, 1), ("partial_fixed", 0, 0),
])
def test_unknown_ambiguous_and_changed_files_fail_closed(tmp_path, mutation, old_count, fixed_count):
    source = seed(tmp_path)
    role = source / ROLE_PATH
    text = role.read_text()
    mutations = {
        "unknown": "<?php\n// unknown source\n",
        "duplicate": text + "\n" + patch._ROLE_OLD,
        "mixed": text + "\n" + patch._ROLE_NEW,
        "changed": text + "\n// changed source\n",
        "fixed_changed": text.replace(patch._ROLE_OLD, patch._ROLE_NEW) + "\n// changed source\n",
        "partial_fixed": text.replace("foreach ($roles as $role)", "foreach ($roles as $roleResult)"),
    }
    role.write_text(mutations[mutation])
    before = role.read_bytes()
    result = patch.execute(str(tmp_path), plan(), "post_source_install", "bad-source")
    assert result["status"] == "error"
    assert result["reason"] == "ambiguous_or_unknown_gate"
    gate = result["patches"][0]["files"][0]
    assert (gate["vulnerable_count"], gate["fixed_count"]) == (old_count, fixed_count)
    assert role.read_bytes() == before
    assert not (tmp_path / ".mcd/patch-runs/bad-source").exists()


def test_713_role_shape_is_not_accepted_as_720(tmp_path):
    source = seed(tmp_path)
    shutil.copyfile(FIXTURES / "Version20211209022550-7.1.3.php", source / ROLE_PATH)
    assert sha(source / ROLE_PATH) == "6cc316e8fc611c58c8b6d9120b6d8b0e2c921c151c42bf29e0faaa141262748b"
    assert patch.execute(str(tmp_path), plan(), "post_source_install", "old-source")["status"] == "error"


@pytest.mark.parametrize("change", ["id", "revision", "schema", "order", "command", "path", "inline_patch", "source", "target"])
def test_untrusted_plan_is_rejected_before_write(tmp_path, change):
    source = seed(tmp_path)
    payload = json.loads(plan())
    if change == "id": payload["patches"][0]["id"] = "UNKNOWN"
    elif change == "revision": payload["registry_revision"] = "0" * 40
    elif change == "schema": payload["schema"] = "unknown"
    elif change == "order": payload["patches"].reverse()
    elif change == "source": payload["source_version"] = "7.1.2"
    elif change == "target": payload["target_version"] = "7.2.1"
    else: payload[change] = "/tmp/arbitrary"
    with pytest.raises(patch.PatchPlanError):
        patch.execute(str(tmp_path), json.dumps(payload), "post_source_install", "bad-plan")
    assert sha(source / ROLE_PATH) == ORIGINAL_SHA


def test_asset_apply_requires_actual_fixed_role(tmp_path):
    seed(tmp_path)
    result = patch.execute(str(tmp_path), plan(), "post_source_install_before_asset_generation", "out-of-order")
    assert result["status"] == "error"
    assert result["reason"] == "dependency_unmet:" + patch.ROLE


def test_wrong_phase_and_install_type(tmp_path):
    seed(tmp_path)
    with pytest.raises(patch.PatchPlanError, match="phase"):
        patch.execute(str(tmp_path), plan(), "after_migrations", "phase")
    with pytest.raises(patch.PatchPlanError, match="install_type"):
        patch.execute(str(tmp_path), plan("zip"), "post_source_install", "layout")


def test_unrelated_720_dependency_does_not_pass_version_gate(tmp_path):
    seed(tmp_path, version="7.1.3")
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [
        {"name": "mautic/core-lib", "version": "7.1.3"},
        {"name": "unrelated/package", "version": "7.2.0"},
    ]}))
    with pytest.raises(patch.PatchPlanError, match="target_version"):
        patch.execute(str(tmp_path), plan(), "post_source_install", "wrong-version")


def test_source_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "instance"
    outside.mkdir(); root.mkdir()
    seed(outside, "zip")
    (root / "docroot").symlink_to(outside, target_is_directory=True)
    (root / "composer.json").write_text('{"name":"mautic/recommended-project"}')
    with pytest.raises(patch.PatchPlanError, match="containment"):
        patch.execute(str(root), plan(), "post_source_install", "escape")


def test_lint_failure_preserves_source_and_backup_evidence(tmp_path, monkeypatch):
    source = seed(tmp_path)
    def fail(_):
        raise patch.PatchPlanError("php_syntax_check_failed")
    monkeypatch.setattr(patch, "_lint", fail)
    result = patch.execute(str(tmp_path), plan(), "post_source_install", "lint-fail")
    assert result["status"] == "error"
    assert sha(source / ROLE_PATH) == ORIGINAL_SHA
    stored = json.loads((tmp_path / ".mcd/patch-runs/lint-fail/result.json").read_text())
    assert stored["backup_records"][0]["before_sha256"] == ORIGINAL_SHA
    assert patch.rollback(str(tmp_path), plan(), "lint-fail")["status"] == "success"


def test_interrupted_apply_requires_recovery_and_preserves_backup(tmp_path, monkeypatch):
    source = seed(tmp_path)
    def interrupt(_):
        raise KeyboardInterrupt
    with monkeypatch.context() as scoped:
        scoped.setattr(patch, "_lint", interrupt)
        with pytest.raises(KeyboardInterrupt):
            patch.execute(str(tmp_path), plan(), "post_source_install", "interrupted")
    with pytest.raises(patch.PatchPlanError, match="incomplete"):
        patch.execute(str(tmp_path), plan(), "post_source_install", "interrupted")
    assert patch.rollback(str(tmp_path), plan(), "interrupted")["status"] == "success"
    assert sha(source / ROLE_PATH) == ORIGINAL_SHA


def test_rollback_rejects_tampered_source_and_backup(tmp_path):
    source = seed(tmp_path)
    result = patch.execute(str(tmp_path), plan(), "post_source_install", "rollback")
    role = source / ROLE_PATH
    patched = role.read_bytes()
    role.write_bytes(patched + b"// unrelated edit\n")
    assert patch.rollback(str(tmp_path), plan(), "rollback")["reason"] == "partial_application"
    role.write_bytes(patched)
    backup = Path(result["patches"][0]["backups"][0]["backup"])
    backup.write_bytes(b"tampered")
    with pytest.raises(patch.PatchPlanError, match="backup_checksum"):
        patch.rollback(str(tmp_path), plan(), "rollback")
    assert role.read_bytes() == patched


def test_php_hydrated_role_behavior_before_and_after(tmp_path):
    source = seed(tmp_path)
    harness = tmp_path / "role-harness.php"
    harness.write_text(r'''<?php
namespace Doctrine\DBAL\Schema { class Schema {} }
namespace Mautic\CoreBundle\Doctrine { abstract class AbstractMauticMigration { public $container; } }
namespace Mautic\UserBundle\Entity {
    class Role { public static int $calls = 0; public function getRawPermissions() { ++self::$calls; return []; } }
}
namespace Mautic\UserBundle\Model {
    class RoleModel { public function getEntities($options) {
        return [[new \Mautic\UserBundle\Entity\Role(), 3], new \Mautic\UserBundle\Entity\Role(), null];
    } }
}
namespace {
    require $argv[1];
    $migration = new \Mautic\Migrations\Version20211209022550();
    $migration->container = new class { public function get($name) { return new \Mautic\UserBundle\Model\RoleModel(); } };
    $migration->postUp(new \Doctrine\DBAL\Schema\Schema());
    echo json_encode(['role_calls' => \Mautic\UserBundle\Entity\Role::$calls]);
}
''')
    cmd = ["php", str(harness), str(source / ROLE_PATH)]
    baseline = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert baseline.returncode != 0
    assert "getRawPermissions() on array" in baseline.stderr + baseline.stdout
    assert patch.execute(str(tmp_path), plan(), "post_source_install", "php")["status"] == "success"
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"role_calls": 2}


@pytest.mark.parametrize("kind", ["zip", "composer"])
@pytest.mark.parametrize("invalid", [False, True])
def test_monolithic_upgrade_emits_gate_before_failure(tmp_path, monkeypatch, capsys, kind, invalid):
    from mcd_agent import mautic_upgrade as upgrade
    source = seed(tmp_path, kind)
    if invalid:
        (source / ROLE_PATH).write_text("<?php\n// unexpected target\n")
    install = SimpleNamespace(runtime="host", root=str(tmp_path), console_path=str(tmp_path / "bin/console"))
    monkeypatch.setattr(upgrade, "_pick_install_record", lambda *a: install)
    versions = iter(["7.1.3", "7.2.0"])
    monkeypatch.setattr(upgrade, "_read_current_version", lambda *a: next(versions))
    for name in ("_require_release_approval", "_enter_upgrade_maintenance", "_exit_upgrade_maintenance",
                 "_pre_upgrade_permissions_check", "ensure_mailer_packages_for_sender_config",
                 "ensure_amazon_mailer_for_bundles", "installed_required_bundles", "_post_upgrade_verify",
                 "_write_upgrade_version_cache"):
        monkeypatch.setattr(upgrade, name, lambda *a, **kw: None)
    monkeypatch.setattr(upgrade, "revert_mautic713_import_tag_patch", lambda *a: {"status": "skip"})
    monkeypatch.setattr(upgrade, "ensure_import_tag_patch", lambda *a: {"status": "skip"})
    monkeypatch.setattr(upgrade, "replace", lambda obj, **kw: obj)
    finished = []
    def source_step(*args):
        args[-1](str(tmp_path))
        finished.append(True)
    monkeypatch.setattr(upgrade, "_apply_zip" if kind == "zip" else "_apply_composer", source_step)
    cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data")
    kwargs = dict(config=cfg, root=str(tmp_path), mode=kind, yes=True, do_backup=False,
                  with_system_upgrade=False, target_override="7.2.0", allow_minor=True,
                  patch_plan_json=plan(kind), patch_run_id="monolithic")
    if invalid:
        with pytest.raises(RuntimeError, match="ambiguous_or_unknown_gate"):
            upgrade.run_upgrade_apply(**kwargs)
    else:
        assert upgrade.run_upgrade_apply(**kwargs) == 0
    evidence = [json.loads(line.split("=", 1)[1]) for line in capsys.readouterr().out.splitlines()
                if line.startswith("MCD_PATCH_PLAN_EVIDENCE=")]
    assert len(evidence) == (1 if invalid else 3)
    assert evidence[0]["patches"][0]["id"] == patch.ROLE
    assert evidence[0]["status"] == ("error" if invalid else "success")
    if invalid:
        assert not finished
        assert evidence[0]["patches"][0]["files"][0]["vulnerable_count"] == 0
    else:
        assert finished
        assert evidence[0]["patches"][0]["gate"]["files"][0]["sha256"] == ORIGINAL_SHA
        assert sha(source / ROLE_PATH) == FIXED_SHA


def test_cli_exact_mcc_plan_and_structured_failure(tmp_path, monkeypatch, capsys):
    from mcd_agent.cli import main
    source = seed(tmp_path)
    args = ["mcd-cli", "mautic-patch-plan", "verify", "--root", str(tmp_path), "--plan-json", plan(),
            "--phase", "post_source_install", "--run-id", "cli", "--json"]
    monkeypatch.setattr("sys.argv", args)
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["patches"][0]["state"] == "vulnerable"
    (source / ROLE_PATH).write_text("<?php\n// unexpected source\n")
    assert main() == 2
    assert json.loads(capsys.readouterr().out)["patches"][0]["state"] == "error"
