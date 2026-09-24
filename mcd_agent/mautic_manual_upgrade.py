"""Admission checks for an explicitly preflighted, root-owned manual upgrade.

This flag is an operator assertion, not an authentication protocol. MCC owns
plugin compatibility, risk acknowledgement and exact single-host job dispatch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcd_agent.install_type import detect_install_type
from mcd_agent.mautic_patch_plan import _RUN, _target_version, parse_plan


class ManualUpgradePreflightError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = str(reason or "manual_preflight_rejected")


def _reject(prefix: str, reason: str, message: str) -> None:
    raise ManualUpgradePreflightError(reason, prefix + message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate patch-plan key")
        result[key] = value
    return result


def _reject_number(value: str) -> None:
    raise ValueError("non-integer patch-plan number")


def validate_preflighted_single_instance(
    *,
    root: str | None,
    install_root: str,
    current: str,
    target: str | None,
    mode: str,
    raw_plan: str | None,
    run_id: str | None,
    yes: bool,
    allow_minor: bool,
    allow_major: bool,
    with_system_upgrade: bool,
) -> None:
    """Validate an explicitly acknowledged, single-instance manual upgrade."""
    prefix = "MCC preflighted single-instance upgrade rejected: "
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        _reject(prefix, "root_execution_required", "root execution is required")
    if not yes or not allow_minor or allow_major or with_system_upgrade:
        _reject(
            prefix,
            "unsafe_upgrade_flags",
            "require --yes/--allow-minor, without major/system upgrade",
        )
    if current == target:
        _reject(prefix, "target_already_installed", f"target {target} is already installed")
    try:
        source_fields = current.split(".")
        target_fields = str(target).split(".")
        if (
            len(source_fields) != 3 or len(target_fields) != 3
            or any(not part.isdigit() for part in (*source_fields, *target_fields))
        ):
            raise ValueError("expected complete semantic versions")
        source_parts = tuple(int(part) for part in source_fields)
        target_parts = tuple(int(part) for part in target_fields)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ManualUpgradePreflightError(
            "unsupported_transition", prefix + "source and target must be complete semantic versions"
        ) from exc
    if source_parts[0] != target_parts[0]:
        _reject(prefix, "unsupported_transition", "manual major-version upgrades are not supported")
    if target_parts <= source_parts:
        _reject(prefix, "unsupported_transition", "manual target must be newer than the verified source")
    if mode not in {"zip", "composer"}:
        _reject(prefix, "unsupported_install_type", "an explicit zip or composer mode is required")
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        _reject(prefix, "invalid_project_root", "an explicit absolute project root is required")
    if raw_plan in (None, ""):
        if run_id not in (None, ""):
            _reject(prefix, "invalid_patch_run_id", "patch-run-id requires a selected patch plan")
        raw_plan = None
    else:
        if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
            _reject(prefix, "invalid_patch_run_id", "a safe patch-run-id is required for a selected patch plan")
        if not isinstance(raw_plan, str) or len(raw_plan.encode("utf-8")) > 16_384:
            _reject(prefix, "invalid_patch_plan", "selected patch plan must be bounded")
    try:
        canonical = Path(root).resolve(strict=True)
        if root != str(canonical) or not canonical.is_dir() or canonical == Path("/"):
            raise ValueError("invocation root is not a canonical project directory")
        if Path(install_root).resolve(strict=True) != canonical:
            raise ValueError("selected instance does not match invocation root")
        if raw_plan is not None:
            decoded = json.loads(
                raw_plan, object_pairs_hook=_unique_object,
                parse_float=_reject_number, parse_constant=_reject_number,
            )
            plan = parse_plan(json.dumps(decoded, ensure_ascii=True, separators=(",", ":")))
            if plan["source_version"] != current or plan["target_version"] != target:
                raise ValueError("patch-plan versions do not match the invocation")
            if plan["install_type"] != mode:
                raise ValueError("patch-plan mode does not match the installed layout")
        if detect_install_type(root) != mode:
            raise ValueError("selected install mode does not match the installed layout")
        if _target_version(canonical) != current:
            raise ValueError("installed source-version metadata is missing or inconsistent")
    except ManualUpgradePreflightError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise ManualUpgradePreflightError("invalid_manual_preflight", prefix + str(exc)) from exc
