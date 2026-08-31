from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


ADAPTER_ROOT = Path("/usr/local/libexec/mcd-runtime-adapters")
_ADAPTER_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


def runtime_adapter_path(name: str, *, root: Path = ADAPTER_ROOT) -> Path:
    token = str(name or "").strip().lower()
    if not _ADAPTER_RE.fullmatch(token):
        raise RuntimeError("invalid runtime migration adapter name")
    path = root / token
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"runtime migration adapter is not installed: {token}")
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022 or not os.access(path, os.X_OK):
        raise RuntimeError(f"runtime migration adapter is not trusted/executable: {token}")
    return path


def installed_runtime_adapters(*, root: Path = ADAPTER_ROOT) -> list[str]:
    if not root.is_dir():
        return []
    out: list[str] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        try:
            runtime_adapter_path(path.name, root=root)
        except RuntimeError:
            continue
        out.append(path.name)
    return out


def run_runtime_adapter(
    name: str,
    *,
    operation: str,
    payload: dict[str, Any],
    timeout_sec: int = 1800,
) -> dict[str, Any]:
    if operation not in {"target-preflight", "target-finalize"}:
        raise RuntimeError("unsupported runtime migration adapter operation")
    path = runtime_adapter_path(name)
    proc = subprocess.run(
        [str(path), operation],
        input=json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        raise RuntimeError(f"runtime migration adapter {name} failed: {detail[-3000:]}")
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runtime migration adapter {name} returned invalid JSON") from exc
    if not isinstance(result, dict) or not bool(result.get("ok")):
        raise RuntimeError(f"runtime migration adapter {name} did not return ok=true")
    return result

