from types import SimpleNamespace
from unittest.mock import patch

from test_mautic_target_patch_verification import VerificationSnapshotTests
from mcd_agent.mautic_target_patch_collector import verify_registered_target


def test_database_fact_original_rejected_before_registry():
    from test_mautic_patch_plan_v3 import _plan
    from mcd_agent.mautic_patch_plan_v3 import _validate_plan
    snapshot, expected, catalog = VerificationSnapshotTests().fixture()
    plan = _plan()
    plan.update(trigger="upgrade_lifecycle", phase="dependency_update_preflight",
        source_version="7.1.3", target_version="7.2.0", execution_context=dict(
            instance_uid="local", application_root="/var/www/fixture", table_prefix="ss_"))
    plan["patches"][0].update(triggers=["upgrade_lifecycle"], phases=["before_cache_warmup"],
        preconditions=[dict(kind="migration_execution_state", table_suffix="migrations",
            version_column="version", migration="DoctrineMigrations\\Version20200101000000",
            encoding="fqcn_utf8", expected="pending")])
    snapshot["original_patch_plan_sha256"] = _validate_plan(plan)
    with patch("mcd_agent.mautic_target_patch_collector.load_readonly") as lookup:
        result = verify_registered_target(SimpleNamespace(), root="/var/www/fixture",
            prepared_target_id=snapshot["prepared_target_id"], snapshot=snapshot,
            catalog_bytes=catalog, original_plan=plan, authorization_context={})
    lookup.assert_not_called()
    assert result["code"] == "input_invalid"


def test_denied_admission_never_restores_or_reads_target():
    snapshot, expected, catalog = VerificationSnapshotTests().fixture()
    context = snapshot["execution_context"]
    local = dict(context=dict(local_instance_uid="local", application_root=context["application_root"],
        table_prefix="ss_"), layout={key: snapshot["root_mapping"][key] for key in
        ("project_root", "application_root_relative", "console_relative_path")}, source_version="7.1.3")
    binding = {key: snapshot[key] for key in ("job_id", "run_id", "execution_context", "identity_association", "root_mapping")}
    binding.update(patch_plan_sha256="a" * 64, source_version="7.1.3", target_version="7.2.0")
    item = dict(state="prepared", receipt=dict(binding=binding, target_artifact={}))
    config = SimpleNamespace(mautic_scenario_registry_file="/private/registry", mcc_url="https://mcc.example", mcc_token="private")
    plan = dict(source_version="7.1.3", target_version="7.2.0", patches=[])
    with patch("mcd_agent.mautic_target_patch_collector._validate_plan", return_value="a" * 64), \
         patch("mcd_agent.mautic_target_patch_collector.load_readonly", return_value=item), \
         patch("mcd_agent.mautic_target_patch_collector.read_local_context", return_value=local), \
         patch("mcd_agent.mautic_target_patch_collector.canonical_json_sha256", return_value="c" * 64), \
         patch("mcd_agent.mautic_target_patch_collector.authorize_target_verification", side_effect=ValueError("denied")), \
         patch("mcd_agent.mautic_prepared_restore.restore_stage") as restore:
        result = verify_registered_target(config, root=context["application_root"],
            prepared_target_id=snapshot["prepared_target_id"], snapshot=snapshot,
            catalog_bytes=catalog, original_plan=plan, authorization_context={})
    restore.assert_not_called()
    assert result["code"] == "authorization_rejected"
