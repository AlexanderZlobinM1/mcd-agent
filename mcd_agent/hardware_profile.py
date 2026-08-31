from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from mcd_agent.mode import (
    _clear_profile_runtime_overrides,
    _read_profile_name,
    _sanitize_on_passive_exit,
    _write_profile_name,
)


PROFILE_SELECTION_FILE = "profile-selection.json"


@dataclass(frozen=True, slots=True)
class HardwareProfileResult:
    mode: str
    previous_profile: str
    profile: str
    cpu_count: int
    memory_kib: int
    changed: bool


def read_memory_kib(path: Path = Path("/proc/meminfo")) -> int:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                return max(0, int(line.split()[1]))
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def recommended_profile(*, cpu_count: int, memory_kib: int) -> str:
    cpus = max(1, int(cpu_count or 1))
    gib = max(0.0, float(memory_kib or 0) / (1024.0 * 1024.0))
    # MemTotal is lower than the provider's nominal RAM because firmware and
    # the kernel reserve part of it. Use conservative lower bounds for the
    # common 4/8/16/32 GiB hardware classes instead of misclassifying every
    # nominal class by one tier (for example, a CX23 reports about 3.73 GiB).
    if cpus <= 1 or (gib and gib < 3.5):
        return "tiny"
    if cpus <= 2 or (gib and gib < 7):
        return "mini"
    if cpus <= 4 or (gib and gib < 14):
        return "midi"
    if cpus <= 8 or (gib and gib < 28):
        return "maxi"
    if cpus >= 24 and (not gib or gib >= 96):
        return "ultra"
    return "hiload"


def recommended_farm_profile(*, cpu_count: int, memory_kib: int) -> str:
    standard = recommended_profile(cpu_count=cpu_count, memory_kib=memory_kib)
    return f"farm-{standard}"


def _state_path(install_dir: str) -> Path:
    return Path(install_dir) / "var" / PROFILE_SELECTION_FILE


def read_profile_selection(install_dir: str) -> dict[str, object]:
    path = _state_path(install_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"mode": "auto"}
    if not isinstance(raw, dict):
        return {"mode": "auto"}
    mode = str(raw.get("mode") or "auto").strip().lower()
    raw["mode"] = mode if mode in {"auto", "manual"} else "auto"
    return raw


def _selection_state_exists(install_dir: str) -> bool:
    path = _state_path(install_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and str(raw.get("mode") or "").strip().lower() in {
        "auto",
        "manual",
    }


def write_profile_selection(
    install_dir: str,
    *,
    mode: str,
    profile: str,
    cpu_count: int | None = None,
    memory_kib: int | None = None,
) -> None:
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in {"auto", "manual"}:
        raise ValueError("profile selection mode must be auto or manual")
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    memory = max(0, int(read_memory_kib() if memory_kib is None else memory_kib))
    path = _state_path(install_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "mode": clean_mode,
        "profile": str(profile or "").strip().lower(),
        "cpu_count": cpus,
        "memory_kib": memory,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def reconcile_hardware_profile(
    *,
    config_path: str,
    install_dir: str = "/opt/mcd",
    cpu_count: int | None = None,
    memory_kib: int | None = None,
) -> HardwareProfileResult:
    cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    memory = max(0, int(read_memory_kib() if memory_kib is None else memory_kib))
    current = _read_profile_name(config_path)
    state = read_profile_selection(install_dir)
    mode = str(state.get("mode") or "auto").strip().lower()
    if current == "farm":
        target = recommended_farm_profile(cpu_count=cpus, memory_kib=memory)
        _write_profile_name(config_path, target)
        _clear_profile_runtime_overrides(config_path)
        write_profile_selection(
            install_dir,
            mode="manual",
            profile=target,
            cpu_count=cpus,
            memory_kib=memory,
        )
        return HardwareProfileResult("manual", current, target, cpus, memory, True)
    if mode == "manual":
        return HardwareProfileResult("manual", current, current, cpus, memory, False)

    state_exists = _selection_state_exists(install_dir)
    recorded_profile = str(state.get("profile") or "").strip().lower()
    # Preserve existing active installations when this state file is introduced:
    # their named profile predates automatic selection and is therefore treated
    # as an operator choice. A direct profile edit after auto mode was recorded
    # is likewise a manual override. `mcd-cli profile auto` explicitly writes an
    # auto marker before calling this function, so operators can always opt in.
    legacy_manual = not state_exists and current != "passive"
    changed_outside_auto = bool(state_exists and recorded_profile and recorded_profile != current)
    if legacy_manual or changed_outside_auto:
        write_profile_selection(
            install_dir,
            mode="manual",
            profile=current,
            cpu_count=cpus,
            memory_kib=memory,
        )
        return HardwareProfileResult("manual", current, current, cpus, memory, False)

    target = recommended_profile(cpu_count=cpus, memory_kib=memory)
    changed = target != current
    if changed:
        was_passive = current == "passive"
        _write_profile_name(config_path, target)
        _clear_profile_runtime_overrides(config_path)
        if was_passive:
            _sanitize_on_passive_exit(config_path)
    write_profile_selection(
        install_dir,
        mode="auto",
        profile=target,
        cpu_count=cpus,
        memory_kib=memory,
    )
    return HardwareProfileResult("auto", current, target, cpus, memory, changed)
