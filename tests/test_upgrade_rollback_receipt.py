from mcd_agent import mautic_patch_plan_v3 as executor
from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from test_mautic_patch_plan_v3 import _plan


def test_upgrade_rollback_receipt_binds_original_apply_plan(tmp_path):
    plan = _plan()
    plan.update(trigger="upgrade_lifecycle", source_version="7.2.0",
                target_version="7.2.1", phase="dependency_update_preflight")
    record = plan["patches"][0]
    record.update(triggers=["upgrade_lifecycle"], phases=["post_source_install"])
    source = tmp_path / record["source_paths"][0]
    source.parent.mkdir(parents=True)
    source.write_bytes(b"before\n")
    applied = executor.execute(str(tmp_path), plan, phase="post_source_install")
    assert applied["status"] == "success"
    receipt = executor.rollback(str(tmp_path), dict(plan, operation="rollback"))
    assert receipt["schema"] == "mcd-mautic-patch-preflight-v3"
    assert receipt["plan_sha256"] == canonical_json_sha256(plan)
    assert receipt["run_id"] == plan["run_id"]
    assert receipt["operation"] == "rollback"
    assert receipt["rollback_succeeded"] is True
    assert source.read_bytes() == b"before\n"
