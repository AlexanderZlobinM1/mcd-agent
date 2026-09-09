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
    """Reject any invocation outside the narrow manual 7.1.3 -> 7.2.0 path."""
    prefix = "MCC preflighted single-instance upgrade rejected: "
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError(prefix + "root execution is required")
    if not yes or not allow_minor or allow_major or with_system_upgrade:
        raise RuntimeError(prefix + "require --yes/--allow-minor, without major/system upgrade")
    if current != "7.1.3" or target != "7.2.0":
        raise RuntimeError(prefix + "only explicit 7.1.3 -> 7.2.0 is supported")
    if mode not in {"zip", "composer"}:
        raise RuntimeError(prefix + "an explicit zip or composer mode is required")
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        raise RuntimeError(prefix + "an explicit absolute project root is required")
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        raise RuntimeError(prefix + "a safe nonempty patch-run-id is required")
    if not isinstance(raw_plan, str) or not raw_plan or len(raw_plan.encode("utf-8")) > 16_384:
        raise RuntimeError(prefix + "an explicit bounded patch plan is required")
    try:
        canonical = Path(root).resolve(strict=True)
        if root != str(canonical) or not canonical.is_dir() or canonical == Path("/"):
            raise ValueError("invocation root is not a canonical project directory")
        if Path(install_root).resolve(strict=True) != canonical:
            raise ValueError("selected instance does not match invocation root")
        decoded = json.loads(
            raw_plan, object_pairs_hook=_unique_object,
            parse_float=_reject_number, parse_constant=_reject_number,
        )
        plan = parse_plan(json.dumps(decoded, ensure_ascii=True, separators=(",", ":")))
        if plan["source_version"] != current or plan["target_version"] != target:
            raise ValueError("patch-plan versions do not match the invocation")
        if plan["install_type"] != mode or detect_install_type(root) != mode:
            raise ValueError("patch-plan mode does not match the installed layout")
        if _target_version(canonical) != current:
            raise ValueError("installed source-version metadata is missing or inconsistent")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise RuntimeError(prefix + str(exc)) from exc
