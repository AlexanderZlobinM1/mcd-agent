import copy
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcd_agent.mautic_patch_exclusion_guard import prove_not_required, upgrade_guard
from mcd_agent.mautic_patch_facts import digest
from test_mautic_patch_plan_v3 import _plan


def plan():
    value = _plan()
    value.update(operation="verify", trigger="upgrade_lifecycle", phase="dependency_update_preflight",
                 source_version="7.2.0", target_version="7.2.1")
    value["execution_context"] = {"instance_uid": "fixture", "application_root": "/fixture", "table_prefix": "ss_"}
    for row in value["patches"]:
        row.update(triggers=["upgrade_lifecycle"], phases=["post_source_install"], preconditions=[
            {"kind": "migration_execution_state", "table_suffix": "migrations", "version_column": "version",
             "migration": "Mautic\\Migrations\\Version20211209022550", "encoding": "fqcn_utf8", "expected": "pending"}])
    return value


def receipt(value, matched=False):
    binding = {key: value[key] for key in ("execution_context", "run_id", "source_version", "target_version", "trigger")}
    binding.update(schema="mcd-mautic-patch-facts-admission-v1", plan_phase=value["phase"],
                   plan_sha256=digest(dict(value, operation="apply")), database_binding_sha256="a" * 64,
                   observation={"fixture": "executed"})
    core = dict(schema="mcd-mautic-patch-facts-admission-v1", observation_binding=binding,
                observations_sha256=digest(binding), records={row["id"]: {"preconditions": matched, "rollback_preconditions": True}
                    for row in value["patches"]})
    invocation = {"operation": "verify", "phase": "post_source_install"}
    return dict(core, invocation=invocation, invocation_sha256=digest(dict(receipt=core, **invocation)))


def test_false_complete_readonly_proof():
    p = plan(); before = copy.deepcopy(p)
    result = prove_not_required(p, lambda *_: receipt(p), "post_source_install")
    assert p == before
    assert result["selected"] == [row["id"] for row in p["patches"]]
    assert all(row["decision"] == "skip_condition_not_required" for row in result["patches"])


@pytest.mark.parametrize("kind", ["true", "unknown", "partial", "phase", "hash"])
def test_fail_closed(kind):
    p = plan(); r = receipt(p, kind == "true")
    if kind == "unknown": r["records"][p["patches"][0]["id"]]["preconditions"] = None
    if kind == "partial": r["records"] = {}
    if kind == "hash": r["observations_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        prove_not_required(p, lambda *_: r, "before_cache_warmup" if kind == "phase" else "post_source_install")


def test_upgrade_rechecks_full_receipt_and_rejects_drift():
    p = plan(); accepted = receipt(p); application = copy.deepcopy(p)
    application["operation"] = "apply"
    for row in application["patches"]:
        row["id"] = "OTHER-FIXTURE-PATCH"
        row.pop("preconditions")
    application.pop("execution_context")
    observations = [accepted, accepted, receipt(p)]
    observations[-1]["observation_binding"]["observation"] = {"fixture": "changed"}
    observations[-1]["observations_sha256"] = digest(observations[-1]["observation_binding"])
    core = {k: v for k, v in observations[-1].items() if k not in {"invocation", "invocation_sha256"}}
    observations[-1]["invocation_sha256"] = digest(dict(receipt=core, **observations[-1]["invocation"]))
    with patch("mcd_agent.mautic_patch_fact_binding.bound_provider", return_value=lambda *_: observations.pop(0)):
        check = upgrade_guard(config=object(), root="/fixture", current=p["source_version"], target=p["target_version"],
            install_type=p["install_type"], run_id=p["run_id"], apply_plan_json=json.dumps(application),
            guard_plan_json=json.dumps(p), receipt=accepted)
        check()
        with pytest.raises(ValueError): check()


def test_upgrade_overlapping_ids_rejected_before_provider():
    p = plan()
    with patch("mcd_agent.mautic_patch_fact_binding.bound_provider") as provider:
        with pytest.raises((ValueError, RuntimeError)):
            upgrade_guard(config=object(), root="/fixture", current=p["source_version"], target=p["target_version"],
                install_type=p["install_type"], run_id=p["run_id"], apply_plan_json=json.dumps(dict(p, operation="apply")),
                guard_plan_json=json.dumps(p), receipt=receipt(p))
        provider.assert_not_called()


@pytest.mark.parametrize("config_args", [[], ["--config", "/fixture/mcd.toml"]])
def test_cli_requires_explicit_false_proof(capsys, config_args):
    from mcd_agent.cli import main
    p = plan()
    with patch.object(sys, "argv", ["mcd-cli", "mautic-patch-plan", "verify", "--root", "/fixture",
            "--plan-json", json.dumps(p), "--run-id", p["run_id"], "--phase", "post_source_install",
            "--require-all-not-required", "--json", *config_args]), patch("mcd_agent.cli.load_config", return_value=object()) as loader, \
            patch("mcd_agent.mautic_patch_fact_binding.bound_provider", return_value=lambda *_: receipt(p)):
        assert main() == 0
    if config_args:
        loader.assert_called_once_with("/fixture/mcd.toml")
    else:
        assert loader.call_args.args[0]
    assert json.loads(capsys.readouterr().out)["facts_receipt"] == receipt(p)


@pytest.mark.parametrize("late", [False, True])
def test_upgrade_guard_rejection_precedes_instance_mutation(late):
    from contextlib import ExitStack
    from mcd_agent import mautic_upgrade as upgrade
    p = plan(); application = copy.deepcopy(p)
    application["operation"] = "apply"
    for row in application["patches"]:
        row["id"] = "OTHER-FIXTURE-PATCH"; row.pop("preconditions")
    application.pop("execution_context")
    inst = SimpleNamespace(root="/fixture", console_path="/fixture/bin/console", runtime="host", instance_uid="fixture")
    cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data", mcc_url="")
    def reject(): raise ValueError("patch_exclusion_drift")
    with ExitStack() as stack:
        for name, value in [("_pick_install_record", inst), ("_read_current_version", "7.2.0"),
                            ("_ensure_upgrade_target_allowed", True), ("_require_release_approval", None),
                            ("_prepare_patch_target_stage", SimpleNamespace(close=lambda: None))]:
            stack.enter_context(patch.object(upgrade, name, return_value=value))
        maintenance = stack.enter_context(patch.object(upgrade, "_enter_upgrade_maintenance"))
        permissions = stack.enter_context(patch.object(upgrade, "_pre_upgrade_permissions_check"))
        stage = stack.enter_context(patch.object(upgrade, "_prepare_patch_target_stage", return_value=SimpleNamespace(close=lambda: None)))
        stack.enter_context(patch("mcd_agent.mautic_patch_resolution.read_plan_file", return_value=json.dumps(p)))
        stack.enter_context(patch("mcd_agent.mautic_patch_exclusion_guard.read_receipt", return_value=receipt(p)))
        stack.enter_context(patch("mcd_agent.mautic_patch_backup.required", return_value=False))
        guard = stack.enter_context(patch("mcd_agent.mautic_patch_exclusion_guard.upgrade_guard",
            **({"return_value": reject} if late else {"side_effect": ValueError("patch_exclusion_true")})))
        with pytest.raises(ValueError):
            upgrade.run_upgrade_apply(config=cfg, root="/fixture", mode=p["install_type"], yes=True,
                do_backup=False, with_system_upgrade=False, target_override="7.2.1",
                patch_plan_json=json.dumps(application), patch_run_id=p["run_id"],
                patch_exclusion_guard_plan_file="/guard", patch_exclusion_guard_plan_sha256="a"*64,
                patch_exclusion_guard_receipt_file="/receipt", patch_exclusion_guard_receipt_sha256="b"*64)
        maintenance.assert_not_called(); permissions.assert_not_called()
        assert stage.called is late
        guard.assert_called_once()


def test_expected_upgrade_migrations_do_not_recompare_old_receipt():
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    from mcd_agent import mautic_upgrade as upgrade
    p = plan(); application = copy.deepcopy(p); application["operation"] = "apply"
    application["install_type"] = p["install_type"] = "zip"
    for row in application["patches"]:
        row["id"] = "OTHER-FIXTURE-PATCH"; row.pop("preconditions")
    application.pop("execution_context")
    inst = SimpleNamespace(root="/fixture", console_path="/fixture/bin/console", runtime="host", instance_uid="fixture")
    cfg = SimpleNamespace(php_bin="php", mautic_run_as_user="www-data", mcc_url="")
    stage = MagicMock()
    # The two pre-mutation rechecks pass. A subsequent comparison of the old
    # snapshot would reject the expected migration executed by this upgrade.
    check = MagicMock(side_effect=[None, None, ValueError("own_migrations_changed_storage")])
    class StopAfterPhase(RuntimeError): pass
    def apply_zip(*args):
        args[-1].apply_phase("/fixture", "post_source_install")
        raise StopAfterPhase()
    with ExitStack() as stack:
        for name, value in [("_pick_install_record", inst), ("_read_current_version", "7.2.0"),
                            ("_ensure_upgrade_target_allowed", True), ("_require_release_approval", None),
                            ("_prepare_patch_target_stage", stage), ("_enter_upgrade_maintenance", object()),
                            ("_pre_upgrade_permissions_check", None), ("_exit_upgrade_maintenance", None)]:
            stack.enter_context(patch.object(upgrade, name, return_value=value))
        stack.enter_context(patch.object(upgrade, "_apply_zip", side_effect=apply_zip))
        stack.enter_context(patch("mcd_agent.mautic_patch_resolution.read_plan_file", return_value=json.dumps(p)))
        stack.enter_context(patch("mcd_agent.mautic_patch_exclusion_guard.read_receipt", return_value=receipt(p)))
        stack.enter_context(patch("mcd_agent.mautic_patch_exclusion_guard.upgrade_guard", return_value=check))
        stack.enter_context(patch("mcd_agent.mautic_patch_backup.required", return_value=False))
        stack.enter_context(patch("mcd_agent.mautic_patch_stage.application_root", side_effect=lambda x: x))
        executor = stack.enter_context(patch("mcd_agent.mautic_patch_plan.execute", return_value={"status": "success"}))
        stack.enter_context(patch("mcd_agent.mautic_patch_plan_v3.rollback", return_value={"status": "success"}))
        with pytest.raises(StopAfterPhase):
            upgrade.run_upgrade_apply(config=cfg, root="/fixture", mode="zip", yes=True,
                do_backup=False, with_system_upgrade=False, target_override="7.2.1",
                patch_plan_json=json.dumps(application), patch_run_id=p["run_id"],
                patch_exclusion_guard_plan_file="/guard", patch_exclusion_guard_plan_sha256="a"*64,
                patch_exclusion_guard_receipt_file="/receipt", patch_exclusion_guard_receipt_sha256="b"*64)
        assert check.call_count == 2
        executor.assert_called_once()
