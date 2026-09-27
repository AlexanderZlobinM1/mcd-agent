"""Read-only reconstruction of an existing registered prepared target."""
from pathlib import Path


def restore_stage(item, *, plan, config, live_root):
    from mcd_agent.mautic_patch_stage import TargetStage, application_root
    from mcd_agent.mautic_patch_plan import _target_version
    from mcd_agent.mautic_patch_fact_binding import needs_facts, bound_provider
    from mcd_agent.mautic_patch_resolution import canonical_json_sha256
    from mcd_agent.composer_prepared_target import _read_regular, recheck_receipt
    local, receipt = item["local"], item["receipt"]
    metadata = local["metadata"]
    fields = {"plan_sha256", "target_version", "hashes", "original_hashes", "original_composer_hashes",
              "application_root_relative", "target_source_sha256", "target_package_sha256", "facts_receipt"}
    if (not isinstance(metadata, dict) or set(metadata) != fields
            or metadata["plan_sha256"] != canonical_json_sha256(plan)
            or metadata["target_version"] != receipt["target_version"]
            or metadata["target_package_sha256"] != receipt["target_artifact"]["manifest"]["composer_lock_sha256"]):
        raise ValueError("prepared stage metadata mismatch")
    if needs_facts(plan) and not isinstance(metadata["facts_receipt"], dict):
        raise ValueError("prepared fact receipt required")
    recheck_receipt(receipt, prepared_root=local["prepared_root"],
        live_root=receipt["binding"]["execution_context"]["application_root"],
        archive_paths=local["archive_paths"], expected_binding=receipt["binding"])
    from mcd_agent.mautic_upgrade import _resolve_composer_project_root
    project_root = _resolve_composer_project_root(live_root)
    anchor = receipt["target_artifact"]["manifest"].get("prepared_source_anchor")
    if anchor is not None and (metadata["hashes"] != anchor["files"]
            or metadata["target_source_sha256"] != anchor["target_source_sha256"]
            or metadata["application_root_relative"] != anchor["application_root_relative"]):
        raise ValueError("prepared metadata differs from signed source anchor")
    stage = TargetStage.__new__(TargetStage)
    stage.directory, stage.root = Path(local["stage_directory"]), Path(local["prepared_root"])
    for key in fields:
        setattr(stage, key, metadata[key])
    stage.root_binding = application_root
    stage.facts_provider = bound_provider(config, live_root) if needs_facts(plan) else None
    stage.prepared_receipt, stage.prepared_binding = receipt, receipt["binding"]
    stage.prepared_archive_paths = local["archive_paths"]
    stage.composer_files = {name: _read_regular(stage.root / name, maximum=16777216, retain=True)
                            for name in ("composer.json", "composer.lock")}
    stage.verify_original(project_root, plan, _target_version)
    return stage
