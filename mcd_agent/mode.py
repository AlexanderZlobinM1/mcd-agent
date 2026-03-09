from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import subprocess

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


MANAGED_KEYWORDS = (
    "mautic:segments:update",
    "mautic:segment:update",
    "mautic:campaigns:update",
    "mautic:campaigns:trigger",
    "mautic:campaign:trigger",
    "mautic:campaigns:rebuild",
    "mautic:campaign:rebuild",
    "mautic:import",
    "doctrine:query:sql",
    "cache:clear",
    "cache:warm",
    "cache:warmup",
    "mautic:emails:send",
)


@dataclass
class ModeResult:
    ok: bool
    lines: list[str]


def _read_crontab(user: str | None = None) -> tuple[int, str]:
    cmd = ["crontab"]
    if user:
        cmd.extend(["-u", user])
    cmd.append("-l")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.returncode, ""
    return 0, proc.stdout or ""


def _write_crontab(content: str, user: str | None = None) -> tuple[int, str]:
    cmd = ["crontab"]
    if user:
        cmd.extend(["-u", user])
    cmd.append("-")
    proc = subprocess.run(cmd, input=content, capture_output=True, text=True)
    out = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, out


def _is_managed_job(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    if "bin/console" not in s:
        return False
    return any(k in s for k in MANAGED_KEYWORDS)


def _comment_managed(content: str, stamp: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if _is_managed_job(line):
            out.append(f"# MCD_MANAGED {stamp}: disabled by mcd profile=active")
            out.append("# " + line)
            changed += 1
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _restore_managed_comments(content: str) -> tuple[str, int]:
    out: list[str] = []
    changed = 0
    expect_line = False
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("# MCD_MANAGED"):
            expect_line = True
            changed += 1
            continue
        if expect_line and line.startswith("# "):
            out.append(line[2:])
            changed += 1
            expect_line = False
            continue
        out.append(line)
        expect_line = False
    return "\n".join(out) + ("\n" if content.endswith("\n") else ""), changed


def _ensure_backup(install_dir: str, user: str, content: str) -> Path:
    backup_dir = Path(install_dir) / "var" / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / f"{user}.pre-active.crontab"
    if not p.exists():
        p.write_text(content, encoding="utf-8")
        os.chmod(p, 0o600)
    return p


def _restore_from_backup(install_dir: str, user: str) -> tuple[bool, str]:
    p = Path(install_dir) / "var" / "backup" / f"{user}.pre-active.crontab"
    if not p.exists():
        return False, f"backup not found for {user}: {p}"
    rc, out = _write_crontab(p.read_text(encoding="utf-8"), None if user == "root" else user)
    if rc != 0:
        return False, f"restore {user} failed: {out}"
    return True, f"restored {user} crontab from backup"


def _profile_file(install_dir: str) -> Path:
    return Path(install_dir) / "var" / "last-active-profile.txt"


def _normalize_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _resolve_mutable_config_path(config_path: str) -> Path:
    """
    Resolve effective writable config file.

    In split-layout entrypoint mode (`mcd.toml` + include files), profile/runtime
    overrides must be edited in local override file (usually `/opt/mcd/etc/mcd.local.toml`)
    and not in package-managed entrypoint.
    """
    p = Path(config_path)
    if not p.exists():
        return p
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return p
    if "MCD_CONFIG_ENTRYPOINT v1" not in text:
        return p
    try:
        raw = tomllib.loads(text)
    except Exception:
        return p
    include = raw.get("include", {})
    files_raw: object = include.get("files", []) if isinstance(include, dict) else []
    files = _normalize_list(files_raw)
    resolved: list[Path] = []
    for item in files:
        fp = Path(item)
        if not fp.is_absolute():
            fp = (p.parent / fp).resolve()
        if fp != p:
            resolved.append(fp)
    for fp in reversed(resolved):
        if fp.exists():
            return fp
    if resolved:
        return resolved[-1]
    return p


def _read_profile_name(config_path: str) -> str:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return "custom"
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^\[profile\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        return "custom"
    body = m.group(1)
    n = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', body)
    if n:
        return n.group(1).strip().lower()
    return "custom"


def _write_profile_name(config_path: str, name: str) -> None:
    p = _resolve_mutable_config_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    section = f'[profile]\nname = "{name}"\n\n'
    m = re.search(r"(?ms)^\[profile\]\s*(.*?)^(?=\[|\Z)", text)
    if not m:
        p.write_text(section + text, encoding="utf-8")
        return
    body = m.group(1)
    if re.search(r'(?m)^\s*name\s*=\s*"[^"]+"', body):
        body2 = re.sub(r'(?m)^\s*name\s*=\s*"[^"]+"', f'name = "{name}"', body, count=1)
    else:
        body2 = f'name = "{name}"\n' + body
    text2 = text[: m.start(1)] + body2 + text[m.end(1) :]
    p.write_text(text2, encoding="utf-8")


def _restart_service(service: str = "mcd") -> tuple[bool, str]:
    proc = subprocess.run(["systemctl", "restart", service], capture_output=True, text=True)
    if proc.returncode == 0:
        return True, f"service restarted: {service}"
    out = (proc.stdout or proc.stderr or "").strip()
    return False, f"service restart failed ({service}): {out}"


def mode_status(*, pause_flag_path: str, config_path: str) -> ModeResult:
    profile = _read_profile_name(config_path)
    paused = Path(pause_flag_path).exists()
    effective_path = _resolve_mutable_config_path(config_path)
    return ModeResult(
        ok=True,
        lines=[
            f"profile={profile}",
            f"pause_flag={pause_flag_path}",
            f"pause_flag_exists={str(paused).lower()}",
            f"config_path={config_path}",
            f"config_mutable_path={effective_path}",
        ],
    )


def mode_activate(*, pause_flag_path: str, install_dir: str, config_path: str) -> ModeResult:
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["profile activate requires root"])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    current_profile = _read_profile_name(config_path)
    target_profile = current_profile
    if current_profile == "passive":
        pf = _profile_file(install_dir)
        if pf.exists():
            target_profile = (pf.read_text(encoding="utf-8").strip() or "tiny").lower()
        else:
            target_profile = "tiny"
    _write_profile_name(config_path, target_profile)
    lines.append(f"profile set: {target_profile}")

    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        updated, changed = _comment_managed(cur, stamp)
        if changed > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed to write crontab: {out2}")
            else:
                lines.append(f"{user}: commented managed cron lines={changed}")
        else:
            lines.append(f"{user}: no managed cron lines found")

    flag = Path(pause_flag_path)
    if flag.exists():
        flag.unlink()
        lines.append(f"legacy pause flag removed: {pause_flag_path}")
    ok, msg = _restart_service("mcd")
    lines.append(msg)
    if not ok:
        return ModeResult(ok=False, lines=lines)
    return ModeResult(ok=True, lines=lines)


def mode_passive(*, pause_flag_path: str, install_dir: str, config_path: str) -> ModeResult:
    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["profile passive requires root"])

    lines: list[str] = []
    current_profile = _read_profile_name(config_path)
    if current_profile != "passive":
        pf = _profile_file(install_dir)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(current_profile, encoding="utf-8")
        lines.append(f"saved previous profile: {current_profile}")
    _write_profile_name(config_path, "passive")
    lines.append("profile set: passive")

    for user in ("root", "www-data"):
        ok, msg = _restore_from_backup(install_dir, user)
        if ok:
            lines.append(msg)
            continue
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        updated, changed = _restore_managed_comments(cur)
        if changed > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed restore from markers: {out2}")
            else:
                lines.append(f"{user}: restored managed cron lines from markers={changed}")
        else:
            lines.append(f"{user}: no managed markers found")

    flag = Path(pause_flag_path)
    if flag.exists():
        flag.unlink()
        lines.append(f"legacy pause flag removed: {pause_flag_path}")
    ok, msg = _restart_service("mcd")
    lines.append(msg)
    if not ok:
        return ModeResult(ok=False, lines=lines)

    return ModeResult(ok=True, lines=lines)


SUPPORTED_PROFILE_NAMES = ("passive", "tiny", "mini", "midi", "maxi", "hiload", "custom")

# Runtime keys controlled by named profiles.
# If these remain in [runtime], they override profile baseline and can cause
# profile drift after any config/deploy reconciliation.
PROFILE_MANAGED_RUNTIME_KEYS = (
    "command_timeout_sec",
    "worker_watchdog_sec",
    "ring_mode",
    "disable_throttle",
    "disable_whitelist",
    "enable_import_polling",
    "enable_campaign_rebuild",
    "segment_priority_weight_threshold",
    "segment_priority_size",
    "campaign_priority_size",
    "campaign_latest_priority_count",
    "segment_priority_parallel_idle",
    "segment_regular_parallel_idle",
    "segment_priority_parallel_throttled",
    "segment_regular_parallel_throttled",
    "segment_throttle_whitelist_only",
    "segment_throttle_whitelist_parallel",
    "segment_throttle_kill_non_whitelist",
    "queue_throttle_threshold",
    "queue_throttle_window_min",
    "campaign_total_parallel",
    "campaign_update_priority_parallel",
    "campaign_update_regular_parallel",
    "campaign_trigger_priority_parallel",
    "campaign_trigger_regular_parallel",
    "campaign_rebuild_priority_parallel",
    "campaign_rebuild_regular_parallel",
)

# Legacy keys used by old config revisions; they are ignored by current runtime
# model and can create operator confusion after passive->active transitions.
LEGACY_RUNTIME_KEYS = (
    "max_parallel_campaigns",
    "max_parallel_segments_idle",
    "max_parallel_segments_active",
    "max_parallel_segments_non_whitelist_active",
    "segment_non_whitelist_policy",
)

# Keep SQL selection defaults owned by current agent runtime when host exits
# passive mode and switches to an active profile.
PROFILE_DEFAULT_SQL_KEYS = (
    "mail_queue_count",
    "segments_due",
    "segment_weights",
    "campaigns_due",
    "import_pending_count",
)


def _clear_profile_runtime_overrides(config_path: str) -> int:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^(\[runtime\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        return 0
    header = m.group(1)
    body = m.group(2)
    removed = 0
    out_lines: list[str] = []
    key_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
    for line in body.splitlines(keepends=True):
        km = key_re.match(line)
        if km and km.group(1) in PROFILE_MANAGED_RUNTIME_KEYS:
            removed += 1
            continue
        out_lines.append(line)
    if removed == 0:
        return 0
    new_body = "".join(out_lines)
    text2 = text[: m.start()] + header + new_body + text[m.end() :]
    p.write_text(text2, encoding="utf-8")
    return removed


def _clear_section_keys(config_path: str, section: str, keys: tuple[str, ...]) -> int:
    p = _resolve_mutable_config_path(config_path)
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^(\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)", text)
    if not m:
        return 0
    header = m.group(1)
    body = m.group(2)
    key_set = set(keys)
    removed = 0
    out_lines: list[str] = []
    key_re = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
    for line in body.splitlines(keepends=True):
        km = key_re.match(line)
        if km and km.group(1) in key_set:
            removed += 1
            continue
        out_lines.append(line)
    if removed == 0:
        return 0
    text2 = text[: m.start()] + header + "".join(out_lines) + text[m.end() :]
    p.write_text(text2, encoding="utf-8")
    return removed


def _sanitize_on_passive_exit(config_path: str) -> tuple[int, int]:
    removed_legacy_runtime = _clear_section_keys(config_path, "runtime", LEGACY_RUNTIME_KEYS)
    removed_sql_overrides = _clear_section_keys(config_path, "sql", PROFILE_DEFAULT_SQL_KEYS)
    return removed_legacy_runtime, removed_sql_overrides


def profile_status(*, pause_flag_path: str, config_path: str) -> ModeResult:
    return mode_status(pause_flag_path=pause_flag_path, config_path=config_path)


def profile_set(*, profile: str, pause_flag_path: str, install_dir: str, config_path: str) -> ModeResult:
    name = (profile or "").strip().lower()
    if name == "passive":
        res = mode_passive(
            pause_flag_path=pause_flag_path,
            install_dir=install_dir,
            config_path=config_path,
        )
        if res.ok:
            removed = _clear_profile_runtime_overrides(config_path)
            if removed:
                res.lines.append(f"runtime overrides cleared for profile baseline: {removed}")
        return res
    if name not in SUPPORTED_PROFILE_NAMES:
        return ModeResult(ok=False, lines=[f"unsupported profile: {profile}"])

    if os.geteuid() != 0:
        return ModeResult(ok=False, lines=["profile apply requires root"])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    current_profile = _read_profile_name(config_path)
    was_passive = current_profile == "passive"
    if was_passive:
        pf = _profile_file(install_dir)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(name, encoding="utf-8")
    _write_profile_name(config_path, name)
    lines.append(f"profile set: {name}")
    if name != "custom":
        removed = _clear_profile_runtime_overrides(config_path)
        if removed:
            lines.append(f"runtime overrides cleared for profile baseline: {removed}")
        if was_passive:
            removed_legacy_runtime, removed_sql_overrides = _sanitize_on_passive_exit(config_path)
            if removed_legacy_runtime:
                lines.append(f"legacy runtime keys cleared on passive exit: {removed_legacy_runtime}")
            if removed_sql_overrides:
                lines.append(f"legacy sql overrides cleared on passive exit: {removed_sql_overrides}")

    for user in ("root", "www-data"):
        rc, cur = _read_crontab(None if user == "root" else user)
        if rc != 0:
            lines.append(f"{user}: crontab not readable, skip")
            continue
        _ensure_backup(install_dir, user, cur)
        updated, changed = _comment_managed(cur, stamp)
        if changed > 0:
            rc2, out2 = _write_crontab(updated, None if user == "root" else user)
            if rc2 != 0:
                lines.append(f"{user}: failed to write crontab: {out2}")
            else:
                lines.append(f"{user}: commented managed cron lines={changed}")
        else:
            lines.append(f"{user}: no managed cron lines found")

    flag = Path(pause_flag_path)
    if flag.exists():
        flag.unlink()
        lines.append(f"legacy pause flag removed: {pause_flag_path}")
    ok, msg = _restart_service("mcd")
    lines.append(msg)
    if not ok:
        return ModeResult(ok=False, lines=lines)
    return ModeResult(ok=True, lines=lines)
