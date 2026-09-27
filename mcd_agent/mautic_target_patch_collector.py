"""Purpose-admitted read-only registered-target collection adapter."""
import time
import os
import hashlib
import stat
from pathlib import Path

from mcd_agent.mautic_prepared_registry import load_readonly
from mcd_agent.mautic_local_context import read_local_context
from mcd_agent.mautic_identity_association import validate_association
from mcd_agent.mautic_patch_plan_v3 import _validate_plan
from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from mcd_agent.mautic_target_patch_verification import validate_snapshot, collect_snapshot, BINDINGS
from mcd_agent.mautic_target_verification_authorization import authorize_target_verification


def rejection(code):
    if code not in {"input_invalid", "binding_mismatch", "authorization_rejected", "target_drift", "fixed_signature_unproven"}:
        raise ValueError("collector_error_code_invalid")
    return dict(schema="mcd-mautic-target-patch-verification-error-v1", status="rejected", code=code)


def _private_bytes(path):
    from mcd_agent.composer_prepared_target import _read_regular
    candidate = Path(path)
    before = candidate.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or stat.S_IMODE(before.st_mode) != 0o600:
        raise ValueError("private input required")
    value = _read_regular(candidate, maximum=1048576, retain=True)
    after = candidate.stat(follow_symlinks=False)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode, s.st_uid)
    if identity(before) != identity(after):
        raise ValueError("private input changed")
    return value


def verify_cli(**arguments):
    if os.geteuid() != 0:
        return rejection("input_invalid")
    try:
        from types import SimpleNamespace
        from mcd_agent.config import (_load_toml_with_includes, _normalize_list,
            _normalize_int_list, _parse_manual_instances)
        from mcd_agent.mautic_check_only_inputs import strict_json
        values = {}
        for name in ("verification_plan", "authorization_context", "patch_plan"):
            value = strict_json(_private_bytes(arguments[name + "_file"]), max_bytes=1048576)
            if not isinstance(value, dict) or canonical_json_sha256(value) != arguments[name + "_sha256"]:
                return rejection("input_invalid")
            values[name] = value
        catalog = _private_bytes(arguments["catalog_file"])
        if hashlib.sha256(catalog).hexdigest() != arguments["catalog_sha256"]:
            return rejection("input_invalid")
        data = _load_toml_with_includes(arguments["config_path"])
        discovery, mcc, runtime = data.get("discovery", {}), data.get("mcc", {}), data.get("runtime", {})
        if any(not isinstance(section, dict) for section in (discovery, mcc, runtime)):
            return rejection("input_invalid")
        config = SimpleNamespace(config_file_path=arguments["config_path"],
            discovery_roots=_normalize_list(discovery.get("roots", ["/var/www"])),
            exclude_path_contains=_normalize_list(discovery.get("exclude_path_contains", [])),
            supported_mautic_majors=_normalize_int_list(discovery.get("supported_mautic_majors", [4, 5, 6, 7])),
            custom_instances=_parse_manual_instances(data.get("instances", [])),
            mcc_url=mcc.get("url"), mcc_token=mcc.get("token"),
            mautic_scenario_registry_file=runtime.get("mautic_scenario_registry_file", "/var/lib/mcd/mautic-scenarios/prepared.sqlite3"))
        return verify_registered_target(config, root=arguments["root"],
            prepared_target_id=arguments["prepared_target_id"], snapshot=values["verification_plan"],
            catalog_bytes=catalog, original_plan=values["patch_plan"], authorization_context=values["authorization_context"])
    except Exception:
        return rejection("input_invalid")


def verify_registered_target(config, *, root, prepared_target_id, snapshot,
                             catalog_bytes, original_plan, authorization_context):
    error_code = "input_invalid"
    try:
        if snapshot["prepared_target_id"] != prepared_target_id:
            return rejection("binding_mismatch")
        original_hash = _validate_plan(original_plan)
        from mcd_agent.mautic_patch_fact_binding import needs_facts
        if needs_facts(original_plan):
            # Reconstruction must not invoke database observers in this command.
            return rejection("input_invalid")
        if original_hash != snapshot["original_patch_plan_sha256"]:
            return rejection("binding_mismatch")
        original_ids = {record["id"] for record in original_plan["patches"]}
        item = load_readonly(config.mautic_scenario_registry_file, prepared_target_id,
            expected_binding=None, now=int(time.time()))
        receipt = item["receipt"]
        binding = receipt["binding"]
        if item["state"] not in {"prepared", "issued"}:
            return rejection("binding_mismatch")
        local = read_local_context(config, root=root)
        context = snapshot["execution_context"]
        if root != context["application_root"]:
            return rejection("binding_mismatch")
        validate_association(snapshot["identity_association"], wire_context=context,
            local_context=local["context"])
        if snapshot["root_mapping"] != dict(schema="mcd-mautic-root-mapping-v1", **local["layout"]):
            return rejection("binding_mismatch")
        if (binding["patch_plan_sha256"] != original_hash
                or binding["source_version"] != original_plan["source_version"]
                or binding["target_version"] != original_plan["target_version"]
                or binding["source_version"] != local["source_version"]
                or any(binding[key] != snapshot[key] for key in
                    ("job_id", "run_id", "execution_context", "identity_association", "root_mapping"))
                or canonical_json_sha256(receipt["target_artifact"]) != snapshot["target_artifact_sha256"]):
            return rejection("binding_mismatch")
        validate_snapshot(snapshot, expected_binding={key: snapshot[key] for key in BINDINGS},
            catalog_bytes=catalog_bytes, original_patch_ids=original_ids)
        error_code = "authorization_rejected"
        authorize_target_verification(snapshot=snapshot, authorization_context=authorization_context,
            mcc_url=config.mcc_url, token=config.mcc_token)
        # No staged-source, Composer or archive reads occur before admission.
        error_code = "target_drift"
        from mcd_agent.mautic_prepared_restore import restore_stage
        stage = restore_stage(item, plan=original_plan, config=config, live_root=root)
        error_code = "fixed_signature_unproven"
        return collect_snapshot(stage, snapshot,
            expected_binding={key: snapshot[key] for key in BINDINGS},
            catalog_bytes=catalog_bytes, original_patch_ids=original_ids)
    except Exception:
        return rejection(error_code)
