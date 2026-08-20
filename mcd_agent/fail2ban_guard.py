from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any


NGINX_4XX_FILTER = Path("/etc/fail2ban/filter.d/nginx-4xx-scan.conf")
NGINX_4XX_JAIL = "nginx-4xx-scan"
MCD_BROWSER_ICON_GUARD_MARKER = "# MCD browser icon false-positive guard"
BROWSER_ICON_IGNORE_REGEX = (
    r'^<HOST>\s+.*"(?:GET|HEAD)\s+/'
    r'(?:apple-touch-icon(?:-precomposed)?\.png|favicon\.ico)'
    r'(?:\?[^"\s]*)?\s+HTTP/[^\"]+"\s+404\s+\d+'
)


def _definition_bounds(lines: list[str]) -> tuple[int, int] | None:
    start: int | None = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped.startswith("[") or not stripped.endswith("]"):
            continue
        if stripped.lower() == "[definition]":
            start = idx + 1
            continue
        if start is not None:
            return start, idx
    if start is None:
        return None
    return start, len(lines)


def _patched_nginx_4xx_filter(text: str) -> tuple[str, bool]:
    if BROWSER_ICON_IGNORE_REGEX in text:
        return text, False

    had_final_newline = text.endswith("\n")
    lines = text.splitlines()
    bounds = _definition_bounds(lines)
    if bounds is None:
        return text, False
    start, end = bounds

    ignore_idx: int | None = None
    ignore_match: re.Match[str] | None = None
    for idx in range(start, end):
        match = re.match(r"^(?P<indent>\s*)ignoreregex\s*=\s*(?P<value>.*)$", lines[idx], re.IGNORECASE)
        if match:
            ignore_idx = idx
            ignore_match = match
            break

    if ignore_idx is None or ignore_match is None:
        insertion = [MCD_BROWSER_ICON_GUARD_MARKER, f"ignoreregex = {BROWSER_ICON_IGNORE_REGEX}"]
        lines[end:end] = insertion
    else:
        indent = ignore_match.group("indent")
        value = ignore_match.group("value").strip()
        marker = indent + MCD_BROWSER_ICON_GUARD_MARKER
        if value:
            lines[ignore_idx:ignore_idx] = [marker]
            lines.insert(ignore_idx + 2, f"{indent}    {BROWSER_ICON_IGNORE_REGEX}")
        else:
            lines[ignore_idx : ignore_idx + 1] = [
                marker,
                f"{indent}ignoreregex = {BROWSER_ICON_IGNORE_REGEX}",
            ]

    patched = "\n".join(lines)
    if had_final_newline:
        patched += "\n"
    return patched, patched != text


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ensure_nginx_4xx_browser_icon_guard(
    *,
    filter_path: Path = NGINX_4XX_FILTER,
    jail: str = NGINX_4XX_JAIL,
) -> dict[str, Any]:
    if not filter_path.exists():
        return {"status": "skipped", "reason": "nginx_4xx_filter_missing", "changed": False}
    if os.geteuid() != 0:
        return {"status": "skipped", "reason": "root_required", "changed": False}

    fail2ban_client = shutil.which("fail2ban-client")
    if not fail2ban_client:
        return {"status": "skipped", "reason": "fail2ban_client_missing", "changed": False}

    original = filter_path.read_text(encoding="utf-8", errors="ignore")
    patched, changed = _patched_nginx_4xx_filter(original)
    if not changed:
        reason = "already_present" if BROWSER_ICON_IGNORE_REGEX in original else "definition_missing"
        return {"status": "noop", "reason": reason, "changed": False}

    mode = filter_path.stat().st_mode & 0o777
    _atomic_write(filter_path, patched, mode)
    reload_proc = subprocess.run(
        [fail2ban_client, "reload", jail],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if reload_proc.returncode != 0:
        _atomic_write(filter_path, original, mode)
        subprocess.run(
            [fail2ban_client, "reload", jail],
            capture_output=True,
            text=True,
            timeout=30,
        )
        detail = (reload_proc.stderr or reload_proc.stdout or f"rc={reload_proc.returncode}").strip()
        return {"status": "error", "reason": f"fail2ban_reload_failed:{detail}", "changed": False}

    return {
        "status": "applied",
        "reason": "browser_icon_404_ignored",
        "changed": True,
        "filter": str(filter_path),
        "jail": jail,
    }
