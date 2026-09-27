"""Read-only local identity and independently resolved Composer layout."""
from pathlib import Path

from mcd_agent.discovery import discover_mautic
from mcd_agent.mautic_patch_fact_binding import _select_instance, discover_execution_context
from mcd_agent.mautic_root_mapping import discover_root_mapping
from mcd_agent.mautic_version_cache import read_mautic_version_evidence_read_only


def read_local_context(config, *, root):
    path = Path(root)
    if not path.is_absolute() or path.is_symlink() or str(path.resolve()) != root:
        raise ValueError("scenario_context_canonical_root_required")
    mapping = discover_root_mapping(root)
    application_root = str(Path(mapping["project_root"]) / mapping["application_root_relative"])
    installs = discover_mautic(config.discovery_roots, config.exclude_path_contains,
        config.supported_mautic_majors, config.custom_instances)
    inst, application = _select_instance(installs, root)
    if str(getattr(inst, "runtime", "host") or "host") != "host" or str(application) != application_root:
        raise ValueError("scenario_context_selected_root_mismatch")
    context = discover_execution_context(config, inst)
    if not isinstance(context, dict) or context.get("application_root") != application_root:
        raise ValueError("scenario_context_discovery_mismatch")
    uid, prefix = context.get("instance_uid"), context.get("table_prefix")
    if type(uid) is not str or not uid or type(prefix) is not str:
        raise ValueError("scenario_context_identity_missing")
    evidence = read_mautic_version_evidence_read_only(mapping["project_root"])
    if evidence.get("source") != "static_metadata":
        raise ValueError("scenario_context_static_version_required")
    return dict(schema="mcd-mautic-local-context-v2",
        context=dict(local_instance_uid=uid, application_root=application_root, table_prefix=prefix),
        layout={key: mapping[key] for key in ("project_root", "application_root_relative", "console_relative_path")},
        source_version=evidence["version"], version_evidence_source="static_metadata")
