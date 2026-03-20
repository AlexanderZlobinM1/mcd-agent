from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


def _run(cmd: list[str], *, timeout_sec: int = 120) -> subprocess.CompletedProcess[str]:
    env = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(5, int(timeout_sec)),
        env={**os.environ, **env},
    )


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int, min_v: int = 0, max_v: int = 86400) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out < min_v:
        out = min_v
    if out > max_v:
        out = max_v
    return out


def _normalize_source_line(line: str) -> str:
    x = line.split("#", 1)[0].strip()
    x = re.sub(r"\s+", " ", x)
    return x


def _collect_list_source_lines() -> dict[str, list[str]]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    out: dict[str, list[str]] = {}
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        lines: list[str] = []
        try:
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                n = _normalize_source_line(raw)
                if not n:
                    continue
                if n.startswith("deb "):
                    lines.append(n)
        except Exception:
            continue
        if lines:
            out[str(p)] = lines
    return out


def detect_duplicate_list_sources() -> dict[str, Any]:
    per_file = _collect_list_source_lines()
    counts: dict[str, int] = {}
    for lines in per_file.values():
        for line in lines:
            counts[line] = counts.get(line, 0) + 1
    duplicates = {k: v for k, v in counts.items() if v > 1}
    return {
        "count": int(sum(v - 1 for v in duplicates.values())),
        "items": [{"source": k, "count": v} for k, v in sorted(duplicates.items())],
    }


def dedupe_list_sources() -> dict[str, Any]:
    files: list[Path] = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*.list")))
    seen: set[str] = set()
    changed_files: list[str] = []
    removed = 0
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        try:
            original = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        new_lines: list[str] = []
        file_changed = False
        for line in original:
            n = _normalize_source_line(line)
            if n.startswith("deb "):
                if n in seen:
                    removed += 1
                    file_changed = True
                    continue
                seen.add(n)
            new_lines.append(line)
        if file_changed:
            p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")
            changed_files.append(str(p))
    return {"changed_files": changed_files, "removed_lines": int(removed)}


def _parse_upgradable_count(raw: str) -> int:
    count = 0
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.lower().startswith("listing"):
            continue
        if x.startswith("WARNING:"):
            continue
        if "/" in x and "upgradable from:" in x:
            count += 1
    return count


def _parse_upgradable_packages(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.lower().startswith("listing"):
            continue
        if x.startswith("WARNING:"):
            continue
        if "/" not in x or "upgradable from:" not in x:
            continue
        parts = x.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("/", 1)[0].strip()
        candidate = str(parts[1]).strip()
        if not name or name in seen:
            continue
        m = re.search(r"\[upgradable from:\s*([^\]]+)\]", x)
        current = m.group(1).strip() if m else ""
        out.append({"name": name, "current": current, "candidate": candidate})
        seen.add(name)
    return out


def _parse_sim_upgrade_packages(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x.startswith("Inst "):
            continue
        parts = x.split()
        if len(parts) < 2:
            continue
        name = str(parts[1]).strip()
        if not name or name in seen:
            continue
        out.append({"name": name, "current": "", "candidate": ""})
        seen.add(name)
    return out


def _parse_phasing_packages(raw: str) -> set[str]:
    out: set[str] = set()
    in_block = False
    for line in (raw or "").splitlines():
        x = line.strip()
        low = x.lower()
        if "deferred due to phasing" in low:
            in_block = True
            continue
        if not in_block:
            continue
        if not x:
            continue
        if re.match(r"^\d+\s+upgraded,\s+\d+\s+newly installed,", x):
            break
        if x.startswith("The following "):
            break
        for tok in x.split():
            name = tok.strip()
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]*$", name):
                out.add(name)
    return out


def _parse_policy_phasing_packages(raw: str) -> set[str]:
    out: set[str] = set()
    current = ""
    for line in (raw or "").splitlines():
        x = line.rstrip()
        s = x.strip()
        if not s:
            continue
        # apt-cache policy block header: "<pkg>:"
        if not x.startswith((" ", "\t")) and s.endswith(":") and "/" not in s:
            current = s[:-1].strip()
            continue
        if not current:
            continue
        if "(phased " in s.lower():
            out.add(current)
    return out


def _policy_phasing_packages(packages: set[str], timeout_sec: int = 45) -> set[str]:
    names = sorted({str(x).strip() for x in packages if str(x).strip()})
    if not names:
        return set()
    out: set[str] = set()
    # Keep command line bounded for safety.
    chunk = 50
    for i in range(0, len(names), chunk):
        part = names[i : i + chunk]
        try:
            p = _run(["apt-cache", "policy", *part], timeout_sec=timeout_sec)
            merged = f"{p.stdout or ''}\n{p.stderr or ''}"
            out.update(_parse_policy_phasing_packages(merged))
        except Exception:
            continue
    return out


def _pending_updates(timeout_sec: int = 45) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    upgradable: list[dict[str, str]] = []
    try:
        p = _run(["apt", "list", "--upgradable"], timeout_sec=timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        cnt = _parse_upgradable_count(merged)
        upgradable = _parse_upgradable_packages(merged)
        if p.returncode not in (0,):
            errors.append(f"apt_list_upgradable_rc_{p.returncode}")
            return (
                {
                    "pending_total": int(cnt),
                    "pending_regular": int(cnt),
                    "pending_phasing": 0,
                    "pending_hold": 0,
                    "pending_updates": int(cnt),
                    "upgradable_packages": upgradable,
                    "phasing_packages": [],
                    "held_packages": [],
                },
                errors,
            )
    except Exception as e:
        errors.append(f"apt_list_upgradable_exception:{e}")

    sim_stdout = ""
    try:
        p2 = _run(["apt-get", "-s", "upgrade"], timeout_sec=timeout_sec)
        sim_stdout = p2.stdout or ""
        if not upgradable:
            upgradable = _parse_sim_upgrade_packages(sim_stdout)
        cnt = len(upgradable)
        if p2.returncode not in (0, 100):
            errors.append(f"apt_get_sim_upgrade_rc_{p2.returncode}")
    except Exception as e:
        errors.append(f"apt_get_sim_upgrade_exception:{e}")
    hold_set: set[str] = set()
    try:
        p3 = _run(["apt-mark", "showhold"], timeout_sec=timeout_sec)
        if p3.returncode == 0:
            hold_set = {x.strip() for x in (p3.stdout or "").splitlines() if x.strip()}
    except Exception:
        hold_set = set()
    phasing_set = _parse_phasing_packages(sim_stdout)
    upgradable_names = {str(row.get("name", "")).strip() for row in upgradable if str(row.get("name", "")).strip()}
    # Ubuntu phased updates often appear as "kept back" without explicit phasing section.
    # Fallback to apt-cache policy phased marker, e.g. "(phased 60%)".
    if upgradable_names:
        policy_phasing = _policy_phasing_packages(upgradable_names, timeout_sec=max(10, int(timeout_sec)))
        phasing_set.update(policy_phasing)
    phasing_set = {x for x in phasing_set if x in upgradable_names}
    hold_set = {x for x in hold_set if x in upgradable_names}

    pending_regular = 0
    pending_pack_rows: list[dict[str, str]] = []
    for row in upgradable:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        tags: list[str] = []
        if name in phasing_set:
            tags.append("phasing")
        if name in hold_set:
            tags.append("hold")
        if not tags:
            pending_regular += 1
        pending_pack_rows.append(
            {
                "name": name,
                "current": str(row.get("current", "")).strip(),
                "candidate": str(row.get("candidate", "")).strip(),
                "state": "+".join(tags) if tags else "regular",
            }
        )

    payload = {
        "pending_total": int(len(pending_pack_rows)),
        "pending_regular": int(pending_regular),
        "pending_phasing": int(len(phasing_set)),
        "pending_hold": int(len(hold_set)),
        # Backward-compatible field consumed by MCC and older clients.
        "pending_updates": int(pending_regular),
        "upgradable_packages": pending_pack_rows[:200],
        "phasing_packages": sorted(phasing_set),
        "held_packages": sorted(hold_set),
    }
    return payload, errors


def _apt_update_errors(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        x = line.strip()
        if not x:
            continue
        if x.startswith("E:"):
            out.append(x)
        elif "NO_PUBKEY" in x or "EXPKEYSIG" in x or "not signed" in x.lower():
            out.append(x)
    dedup: list[str] = []
    seen: set[str] = set()
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        dedup.append(x)
    return dedup[:20]


def _has_ppa_marker(marker: str) -> bool:
    marker_l = marker.strip().lower()
    if not marker_l:
        return False
    files = [Path("/etc/apt/sources.list")]
    files.extend(sorted(Path("/etc/apt/sources.list.d").glob("*")))
    for p in files:
        if not p.exists() or not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        if marker_l in txt:
            return True
    return False


def _ensure_ppa(ppa: str, *, timeout_sec: int) -> tuple[bool, str]:
    add_repo = Path("/usr/bin/add-apt-repository")
    if not add_repo.exists():
        return False, "add-apt-repository not found"
    cmd = [str(add_repo), "-y", f"ppa:{ppa}"]
    p = _run(cmd, timeout_sec=timeout_sec)
    if p.returncode == 0:
        return True, f"added ppa:{ppa}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"add ppa:{ppa} failed"


def _run_mariadb_repo_setup(version: str, *, timeout_sec: int) -> tuple[bool, str]:
    v = str(version or "").strip() or "mariadb-11.4"
    script = (
        "set -euo pipefail; "
        "curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup "
        f"| bash -s -- --mariadb-server-version=\"{v}\""
    )
    p = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(30, int(timeout_sec)),
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"},
    )
    if p.returncode == 0:
        return True, f"mariadb_repo_setup:{v}"
    msg = (p.stderr or p.stdout or "").strip()
    return False, msg or f"mariadb_repo_setup:{v} failed"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zbx_marker_path(cfg: Any | None = None) -> Path:
    if cfg is not None:
        state_db = str(getattr(cfg, "state_db_path", "") or "").strip()
        if state_db:
            return Path(state_db).parent / "zabbix-mysql-bootstrap.json"
    return Path("/opt/mcd/var/zabbix-mysql-bootstrap.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mysql_client_bin() -> str | None:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _sql_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "''")


def _normalize_grants(raw: Any) -> list[str]:
    default = ["REPLICATION CLIENT", "PROCESS", "SHOW DATABASES", "SHOW VIEW"]
    if not isinstance(raw, list):
        return default
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().upper()
        if not s:
            continue
        if not re.match(r"^[A-Z_ ]+$", s):
            continue
        out.append(s)
    return out or default


def _run_mysql_root_sql(sql: str, *, timeout_sec: int = 20) -> tuple[bool, str]:
    mysql_bin = _mysql_client_bin()
    if not mysql_bin:
        return False, "mysql_client_not_found"
    cmd = [mysql_bin, "--batch", "--skip-column-names", "--protocol=socket", "-e", sql]
    proc = _run(cmd, timeout_sec=timeout_sec)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or f"mysql_exec_failed_rc_{proc.returncode}"


def _mysql_user_exists(user: str, host: str, *, timeout_sec: int = 12) -> tuple[bool | None, str]:
    sql = (
        "SELECT 1 FROM mysql.user "
        f"WHERE User='{_sql_escape(user)}' AND Host='{_sql_escape(host)}' LIMIT 1;"
    )
    ok, detail = _run_mysql_root_sql(sql, timeout_sec=timeout_sec)
    if not ok:
        return None, detail
    exists = any(x.strip() == "1" for x in (detail or "").splitlines())
    return exists, ""


def collect_zabbix_mysql_monitor_state(*, cfg: Any | None = None) -> dict[str, Any]:
    marker_path = _zbx_marker_path(cfg)
    marker = _read_json(marker_path)
    out = {
        "status": str(marker.get("last_status", "unknown") or "unknown"),
        "applied": bool(marker.get("applied", False)),
        "user": str(marker.get("user", "zbx_monitor") or "zbx_monitor"),
        "host": str(marker.get("host", "127.0.0.1") or "127.0.0.1"),
        "attempted_at_utc": str(marker.get("attempted_at_utc", "") or ""),
        "applied_at_utc": str(marker.get("applied_at_utc", "") or ""),
        "last_error": str(marker.get("last_error", "") or ""),
        "marker_path": str(marker_path),
    }
    return out


def ensure_zabbix_mysql_monitor_user(
    profile: dict[str, Any] | None,
    *,
    cfg: Any | None = None,
    force: bool = False,
    timeout_sec: int = 20,
) -> dict[str, Any]:
    prof = profile if isinstance(profile, dict) else {}
    enabled = _bool(prof.get("zabbix_mysql_monitor_enabled"), True)
    user = str(prof.get("zabbix_mysql_monitor_user", "zbx_monitor") or "zbx_monitor").strip() or "zbx_monitor"
    host = str(prof.get("zabbix_mysql_monitor_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
    password = str(prof.get("zabbix_mysql_monitor_password", "zbx_monitor") or "zbx_monitor")
    grants = _normalize_grants(prof.get("zabbix_mysql_monitor_grants"))
    apply_once = _bool(prof.get("zabbix_mysql_monitor_apply_once"), True)
    marker_path = _zbx_marker_path(cfg)
    marker = _read_json(marker_path)

    if not enabled:
        result = {
            "status": "disabled",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "reason": "zabbix_mysql_monitor_disabled",
        }
        marker.update(
            {
                "last_status": "disabled",
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "last_error": "",
            }
        )
        _write_json(marker_path, marker)
        return result

    if apply_once and not force and bool(marker.get("applied", False)):
        return {
            "status": "noop",
            "reason": "already_applied_once",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "applied_at_utc": str(marker.get("applied_at_utc", "") or ""),
        }

    exists_before, exists_err = _mysql_user_exists(user, host, timeout_sec=max(5, int(timeout_sec)))
    if exists_before is True:
        marker.update(
            {
                "last_status": "already_present",
                "applied": True,
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "applied_at_utc": str(marker.get("applied_at_utc") or _now_utc_iso()),
                "last_error": "",
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "already_present",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "user_exists_before": True,
            "user_exists_after": True,
        }
    if exists_before is None:
        marker.update(
            {
                "last_status": "error",
                "applied": bool(marker.get("applied", False)),
                "user": user,
                "host": host,
                "attempted_at_utc": _now_utc_iso(),
                "last_error": str(exists_err or "mysql_user_probe_failed"),
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": str(exists_err or "mysql_user_probe_failed"),
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    if apply_once and not force and bool(marker.get("attempted_once", False)):
        return {
            "status": "skipped",
            "reason": "already_attempted_once",
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
            "last_status": str(marker.get("last_status", "") or ""),
            "last_error": str(marker.get("last_error", "") or ""),
        }

    grants_sql = ", ".join(grants)
    sql = "\n".join(
        [
            f"CREATE USER IF NOT EXISTS '{_sql_escape(user)}'@'{_sql_escape(host)}' IDENTIFIED BY '{_sql_escape(password)}';",
            f"GRANT {grants_sql} ON *.* TO '{_sql_escape(user)}'@'{_sql_escape(host)}';",
            "FLUSH PRIVILEGES;",
        ]
    )
    ok, detail = _run_mysql_root_sql(sql, timeout_sec=max(8, int(timeout_sec)))
    attempted_at = _now_utc_iso()
    if not ok:
        marker.update(
            {
                "last_status": "error",
                "applied": bool(marker.get("applied", False)),
                "user": user,
                "host": host,
                "attempted_at_utc": attempted_at,
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": str(detail or "mysql_exec_failed"),
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": str(detail or "mysql_exec_failed"),
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    exists_after, verify_err = _mysql_user_exists(user, host, timeout_sec=max(5, int(timeout_sec)))
    if exists_after is not True:
        reason = str(verify_err or "mysql_user_not_visible_after_apply")
        marker.update(
            {
                "last_status": "error",
                "applied": False,
                "user": user,
                "host": host,
                "attempted_at_utc": attempted_at,
                "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
                "last_error": reason,
            }
        )
        _write_json(marker_path, marker)
        return {
            "status": "error",
            "reason": reason,
            "user": user,
            "host": host,
            "marker_path": str(marker_path),
        }

    marker.update(
        {
            "last_status": "applied",
            "applied": True,
            "user": user,
            "host": host,
            "attempted_at_utc": attempted_at,
            "applied_at_utc": _now_utc_iso(),
            "attempted_once": bool(apply_once) or bool(marker.get("attempted_once", False)),
            "last_error": "",
        }
    )
    _write_json(marker_path, marker)
    return {
        "status": "applied",
        "user": user,
        "host": host,
        "marker_path": str(marker_path),
        "user_exists_before": False,
        "user_exists_after": True,
        "grants": grants,
    }


def collect_apt_state(*, timeout_sec: int = 45, cfg: Any | None = None) -> dict[str, Any]:
    pending_info, pending_err = _pending_updates(timeout_sec=max(10, int(timeout_sec)))
    pending = int(pending_info.get("pending_updates", 0) or 0)
    pending_total = int(pending_info.get("pending_total", pending) or pending)
    pending_phasing = int(pending_info.get("pending_phasing", 0) or 0)
    pending_hold = int(pending_info.get("pending_hold", 0) or 0)
    duplicates = detect_duplicate_list_sources()
    errors: list[str] = list(pending_err)
    dup_count = int(duplicates.get("count", 0) or 0)
    if dup_count > 0:
        errors.append(f"duplicate_apt_sources:{dup_count}")

    status = "ok"
    if errors:
        status = "error"
    elif pending > 0:
        status = "updates_pending"
    elif pending_phasing > 0 or pending_hold > 0:
        status = "updates_deferred"

    if status == "ok":
        level = 0
    elif status == "updates_deferred":
        level = 2
    else:
        level = 5
    return {
        "status": status,
        "level": level,
        "pending_total": int(pending_total),
        "pending_updates": int(pending),
        "pending_regular": int(pending),
        "pending_phasing": int(pending_phasing),
        "pending_hold": int(pending_hold),
        "error_count": int(len(errors)),
        "errors": errors[:20],
        "duplicate_sources": duplicates,
        "upgradable_packages": list(pending_info.get("upgradable_packages", []) or [])[:200],
        "phasing_packages": list(pending_info.get("phasing_packages", []) or [])[:200],
        "held_packages": list(pending_info.get("held_packages", []) or [])[:200],
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zabbix_mysql_monitor": collect_zabbix_mysql_monitor_state(cfg=cfg),
    }


def apply_apt_profile(
    profile: dict[str, Any],
    *,
    dry_run: bool = False,
    cfg: Any | None = None,
    force_zabbix_bootstrap: bool = False,
) -> dict[str, Any]:
    if profile is None:
        profile = {}
    if dry_run:
        current = collect_apt_state(timeout_sec=25, cfg=cfg)
        return {
            "status": "planned",
            "actions": [
                "remove_third_party_sources",
                "dedupe_list_sources",
                "repair_repos_on_error",
                "apt_update",
                "optional_package_ops",
                "zabbix_mysql_monitor_bootstrap",
            ],
            "current": current,
        }

    actions: list[str] = []
    changed: list[str] = []
    errors: list[str] = []

    refresh_cache = _bool(profile.get("refresh_cache"), True)
    refresh_timeout_sec = _int(profile.get("refresh_timeout_sec"), 180, 30, 3600)
    repair_on_error = _bool(profile.get("repair_on_error"), True)
    remove_third_party = _bool(profile.get("remove_third_party_sources"), True)
    dedupe_sources = _bool(profile.get("dedupe_list_sources"), True)
    mariadb_repair = _bool(profile.get("mariadb_repo_setup_enabled"), True)
    mariadb_version = str(profile.get("mariadb_repo_setup_version", "mariadb-11.4"))
    ensure_php_ppa = _bool(profile.get("ensure_ondrej_php_ppa"), False)
    ensure_nginx_ppa = _bool(profile.get("ensure_ondrej_nginx_ppa"), False)
    ensure_apache_ppa = _bool(profile.get("ensure_ondrej_apache_ppa"), False)
    upgrade_mode = str(profile.get("upgrade_mode", "none")).strip().lower() or "none"
    packages_present = [str(x).strip() for x in list(profile.get("packages_present", []) or []) if str(x).strip()]
    packages_absent = [str(x).strip() for x in list(profile.get("packages_absent", []) or []) if str(x).strip()]

    if remove_third_party:
        p = Path("/etc/apt/sources.list.d/third-party.sources")
        if p.exists():
            p.unlink()
            changed.append(str(p))
            actions.append("removed:/etc/apt/sources.list.d/third-party.sources")

    if dedupe_sources:
        dedupe = dedupe_list_sources()
        removed = int(dedupe.get("removed_lines", 0) or 0)
        if removed > 0:
            actions.append(f"dedupe_list_sources:removed={removed}")
        for f in list(dedupe.get("changed_files", []) or []):
            changed.append(str(f))

    def _run_update() -> list[str]:
        if not refresh_cache:
            return []
        p = _run(["apt-get", "update"], timeout_sec=refresh_timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        errs = _apt_update_errors(merged)
        if p.returncode != 0 and not errs:
            errs = [f"apt-get update failed rc={p.returncode}"]
        return errs

    update_errors = _run_update()
    if update_errors:
        errors.extend(update_errors)

    if repair_on_error and update_errors:
        fixed_any = False
        txt = "\n".join(update_errors).lower()
        if mariadb_repair and ("mariadb" in txt or "r.mariadb.com" in txt):
            ok, msg = _run_mariadb_repo_setup(mariadb_version, timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)

        if ensure_php_ppa and ("ondrej" in txt or "php" in txt) and not _has_ppa_marker("ondrej/php"):
            ok, msg = _ensure_ppa("ondrej/php", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)
        if ensure_nginx_ppa and ("ondrej" in txt or "nginx" in txt) and not _has_ppa_marker("ondrej/nginx"):
            ok, msg = _ensure_ppa("ondrej/nginx", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)
        if ensure_apache_ppa and ("ondrej" in txt or "apache" in txt or "apache2" in txt) and not _has_ppa_marker("ondrej/apache2"):
            ok, msg = _ensure_ppa("ondrej/apache2", timeout_sec=refresh_timeout_sec)
            actions.append(msg)
            if ok:
                fixed_any = True
            else:
                errors.append(msg)

        if fixed_any:
            retry_errors = _run_update()
            errors = [e for e in errors if not e.startswith("E:") and "apt-get update failed" not in e]
            errors.extend(retry_errors)
            if not retry_errors:
                actions.append("apt_update_repair_ok")

    if packages_present:
        cmd = ["apt-get", "install", "-y"] + packages_present
        p = _run(cmd, timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append(f"packages_present:{len(packages_present)}")
        else:
            errors.append((p.stderr or p.stdout or "apt install failed").strip())
    if packages_absent:
        cmd = ["apt-get", "purge", "-y"] + packages_absent
        p = _run(cmd, timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append(f"packages_absent:{len(packages_absent)}")
        else:
            errors.append((p.stderr or p.stdout or "apt purge failed").strip())

    if upgrade_mode in {"safe", "upgrade"}:
        p = _run(["apt-get", "upgrade", "-y"], timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append("upgrade:upgrade")
        else:
            errors.append((p.stderr or p.stdout or "apt upgrade failed").strip())
    elif upgrade_mode in {"full", "dist", "dist-upgrade"}:
        p = _run(["apt-get", "dist-upgrade", "-y"], timeout_sec=refresh_timeout_sec)
        if p.returncode == 0:
            actions.append("upgrade:dist-upgrade")
        else:
            errors.append((p.stderr or p.stdout or "apt dist-upgrade failed").strip())

    zbx_result = ensure_zabbix_mysql_monitor_user(
        profile,
        cfg=cfg,
        force=bool(force_zabbix_bootstrap),
        timeout_sec=max(8, int(refresh_timeout_sec)),
    )
    zbx_status = str(zbx_result.get("status", "") or "").strip().lower()
    if zbx_status in {"applied", "already_present", "noop", "disabled", "skipped"}:
        actions.append(f"zabbix_mysql_monitor:{zbx_status}")
    elif zbx_status == "error":
        errors.append(f"zabbix_mysql_monitor:{str(zbx_result.get('reason', 'unknown_error'))}")
    else:
        actions.append(f"zabbix_mysql_monitor:{zbx_status or 'unknown'}")

    state = collect_apt_state(timeout_sec=45, cfg=cfg)
    state["zabbix_mysql_monitor"] = zbx_result
    if errors:
        # Preserve explicit action errors together with state-derived errors.
        merged = list(errors)
        for e in list(state.get("errors", []) or []):
            if e not in merged:
                merged.append(e)
        state["errors"] = merged[:40]
        state["error_count"] = int(len(merged))
        state["status"] = "error"
        state["level"] = 5

    return {
        "status": "applied",
        "actions": actions,
        "changed_files": changed,
        "state": state,
    }
