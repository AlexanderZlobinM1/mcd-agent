from __future__ import annotations

import logging
from pathlib import Path
import re
import shlex
import subprocess

from mcd_agent.fs_permissions import ensure_instance_permissions

SUPPORTED_COMMANDS = {
    "campaign:trigger": "mautic:campaign:trigger",
    "segments:update": "mautic:segments:update",
    "campaign:rebuild": "mautic:campaign:rebuild",
    # Keep backward-compatible CLI alias: update == rebuild.
    "campaigns:update": "mautic:campaigns:rebuild",
    "campaigns:trigger": "mautic:campaigns:trigger",
    "import": "mautic:import",
    "cache:clear": "cache:clear",
    "cache:warmup": "cache:warmup",
}

COMMAND_TASK_TYPES = {
    "campaign:trigger": "campaign_trigger",
    "campaigns:trigger": "campaign_trigger",
    "segments:update": "segment",
    "campaign:rebuild": "campaign_rebuild",
    "campaigns:update": "campaign_rebuild",
    "import": "import",
}

_CACHE_COMMANDS = {"cache:clear", "cache:warmup"}
_PERMISSION_DENIED_RE = re.compile(r"permission denied", re.IGNORECASE)
_CACHE_PERM_WARNING_MARKER = "MCD_WARNING_CACHE_PERMISSIONS_REPAIRED"


def _resolve_console_path(root: str) -> str | None:
    root_path = Path(root)
    console_bin = root_path / "bin" / "console"
    console_legacy = root_path / "app" / "console"

    if console_bin.exists():
        return str(console_bin)
    if console_legacy.exists():
        return str(console_legacy)
    return None


def render_mautic_command(
    *,
    php_bin: str,
    root: str,
    template: str,
    run_as_user: str | None = None,
    **params: object,
) -> list[str]:
    console = _resolve_console_path(root)
    if not console:
        raise FileNotFoundError(f"Console not found in root: {root}")

    rendered = template.format(**params)
    parts = shlex.split(rendered)
    cmd = [php_bin, console] + parts + ["--no-interaction"]
    if run_as_user:
        cmd = ["sudo", "-u", run_as_user] + cmd
    return cmd


def command_task_type(command: str) -> str | None:
    return COMMAND_TASK_TYPES.get(command)


def build_mautic_exec_args(
    *,
    php_bin: str,
    root: str,
    command: str,
    instance_id: int | None,
    run_as_user: str | None = None,
) -> list[str]:
    if command not in SUPPORTED_COMMANDS:
        raise ValueError(f"Unsupported command: {command}")

    template = SUPPORTED_COMMANDS[command]
    if command in {"campaign:trigger", "segments:update", "campaign:rebuild", "campaigns:update", "campaigns:trigger"} and instance_id is not None:
        template += f" -i {instance_id}"
    return render_mautic_command(
        php_bin=php_bin,
        root=root,
        template=template,
        run_as_user=run_as_user,
    )


def execute_mautic_command(
    *,
    php_bin: str,
    root: str,
    command: str,
    instance_id: int | None,
    timeout_sec: int,
    run_as_user: str | None = None,
) -> tuple[int, str]:
    try:
        cmd = build_mautic_exec_args(
            php_bin=php_bin,
            root=root,
            command=command,
            instance_id=instance_id,
            run_as_user=run_as_user,
        )
    except ValueError as e:
        return 2, str(e)
    except FileNotFoundError as e:
        return 3, str(e)

    rc, output = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
    if command in _CACHE_COMMANDS and rc != 0 and _looks_like_permission_error(output):
        repaired, repair_note = _repair_cache_permissions(root=root, run_as_user=run_as_user)
        rc2, output2 = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
        combined = _combine_output(output, repair_note, output2)
        if rc2 == 0:
            marker = f"{_CACHE_PERM_WARNING_MARKER}: command={command}"
            if combined:
                combined = marker + "\n" + combined
            else:
                combined = marker
            return 0, combined
        if repaired:
            logging.warning(
                "cache command failed after auto-repair root=%s command=%s rc=%s",
                root,
                command,
                rc2,
            )
        return rc2, combined
    return rc, output


def execute_mautic_command_template(
    *,
    php_bin: str,
    root: str,
    template: str,
    timeout_sec: int,
    run_as_user: str | None = None,
    **params: object,
) -> tuple[int, str]:
    rendered = str(template).format(**params)
    command_name = _template_command_name(rendered)
    try:
        cmd = render_mautic_command(
            php_bin=php_bin,
            root=root,
            template=rendered,
            run_as_user=run_as_user,
        )
    except FileNotFoundError as e:
        return 3, str(e)

    rc, output = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
    if command_name in _CACHE_COMMANDS and rc != 0 and _looks_like_permission_error(output):
        repaired, repair_note = _repair_cache_permissions(root=root, run_as_user=run_as_user)
        rc2, output2 = _run_cmd(cmd=cmd, root=root, timeout_sec=timeout_sec)
        combined = _combine_output(output, repair_note, output2)
        if rc2 == 0:
            marker = f"{_CACHE_PERM_WARNING_MARKER}: command={command_name}"
            if combined:
                combined = marker + "\n" + combined
            else:
                combined = marker
            return 0, combined
        if repaired:
            logging.warning(
                "cache template command failed after auto-repair root=%s command=%s rc=%s",
                root,
                command_name,
                rc2,
            )
        return rc2, combined
    return rc, output


def _run_cmd(*, cmd: list[str], root: str, timeout_sec: int) -> tuple[int, str]:
    run_timeout = timeout_sec if timeout_sec and timeout_sec > 0 else None
    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=run_timeout,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return int(proc.returncode), output.strip()


def _looks_like_permission_error(output: str) -> bool:
    return bool(_PERMISSION_DENIED_RE.search(str(output or "")))


def _repair_cache_permissions(*, root: str, run_as_user: str | None) -> tuple[bool, str]:
    user = str(run_as_user or "www-data").strip() or "www-data"
    try:
        repaired = ensure_instance_permissions(root=root, run_as_user=user)
    except Exception as e:
        logging.warning("cache permission auto-repair failed root=%s user=%s: %s", root, user, e)
        return False, f"MCD_WARN: cache-permissions repair failed ({e})"

    fixed = bool(repaired.repaired_paths or repaired.console_exec_fixed)
    details: list[str] = []
    details.append(
        "MCD_WARN: cache-permissions repair attempted "
        f"(fixed_paths={len(repaired.repaired_paths)} console_exec_fixed={1 if repaired.console_exec_fixed else 0})"
    )
    if repaired.repaired_paths:
        details.append("MCD_WARN: repaired paths: " + ", ".join(sorted(set(repaired.repaired_paths))))
    if repaired.errors:
        details.append("MCD_WARN: repair errors: " + "; ".join(repaired.errors))

    if fixed:
        logging.warning(
            "cache permission auto-repair applied root=%s user=%s repaired_paths=%s console_exec_fixed=%s",
            root,
            user,
            ",".join(sorted(set(repaired.repaired_paths))),
            repaired.console_exec_fixed,
        )
    return fixed, "\n".join(details)


def _template_command_name(rendered_template: str) -> str:
    parts = shlex.split(str(rendered_template or ""))
    if not parts:
        return ""
    return str(parts[0]).strip().lower()


def _combine_output(*parts: str) -> str:
    out = [str(x).strip() for x in parts if str(x).strip()]
    return "\n".join(out).strip()
