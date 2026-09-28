"""Verify an already completed backup bound to a signed JSON repair context."""

from __future__ import annotations

from mcd_agent.mautic_json_repair import (
    load_authorization_context,
    validate_authorization_context,
    verify_backup_evidence,
)
from mcd_agent.mautic_upgrade_contract import validate_json_repair_plan


def release_backup_satisfied(
    *,
    do_backup: bool,
    requires_json_repair: bool,
    repair_plan_json: str | None,
    repair_auth_context_file: str | None,
    repair_auth_key_file: str,
    instance_uid: str,
    root: str,
    source_version: str,
    target_version: str,
) -> bool:
    """Accept a local backup or a signed, still-verifiable external backup."""
    if do_backup:
        return True
    if not requires_json_repair or not repair_plan_json or not repair_auth_context_file:
        return False
    plan = validate_json_repair_plan(repair_plan_json)
    context, signing_key = load_authorization_context(
        repair_auth_context_file, key_path=repair_auth_key_file,
    )
    validate_authorization_context(
        context,
        plan=plan,
        instance_uid=instance_uid,
        root=root,
        source_version=source_version,
        target_version=target_version,
        signing_key=signing_key,
    )
    verified = verify_backup_evidence(
        context["backup_evidence"], root=root, instance_uid=instance_uid,
    )
    return verified["rollback_supported"] is True
