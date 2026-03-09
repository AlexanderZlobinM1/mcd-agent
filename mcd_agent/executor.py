from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

SUPPORTED_COMMANDS = {
    "campaign:trigger": "mautic:campaign:trigger",
    "segments:update": "mautic:segments:update",
    "campaign:rebuild": "mautic:campaign:rebuild",
    # Keep backward-compatible CLI alias: update == rebuild.
    "campaigns:update": "mautic:campaigns:rebuild",
    "campaigns:trigger": "mautic:campaigns:trigger",
    "import": "mautic:import",
}

COMMAND_TASK_TYPES = {
    "campaign:trigger": "campaign_trigger",
    "campaigns:trigger": "campaign_trigger",
    "segments:update": "segment",
    "campaign:rebuild": "campaign_rebuild",
    "campaigns:update": "campaign_rebuild",
    "import": "import",
}


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

    run_timeout = timeout_sec if timeout_sec and timeout_sec > 0 else None
    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=run_timeout,
    )

    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, output.strip()


def execute_mautic_command_template(
    *,
    php_bin: str,
    root: str,
    template: str,
    timeout_sec: int,
    run_as_user: str | None = None,
    **params: object,
) -> tuple[int, str]:
    try:
        cmd = render_mautic_command(
            php_bin=php_bin,
            root=root,
            template=template,
            run_as_user=run_as_user,
            **params,
        )
    except FileNotFoundError as e:
        return 3, str(e)

    run_timeout = timeout_sec if timeout_sec and timeout_sec > 0 else None
    proc = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=run_timeout,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, output.strip()
