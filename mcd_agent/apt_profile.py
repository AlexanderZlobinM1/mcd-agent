from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
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


def _pending_updates(timeout_sec: int = 45) -> tuple[int, list[str]]:
    errors: list[str] = []
    try:
        p = _run(["apt", "list", "--upgradable"], timeout_sec=timeout_sec)
        merged = f"{p.stdout or ''}\n{p.stderr or ''}"
        cnt = _parse_upgradable_count(merged)
        if p.returncode not in (0,):
            errors.append(f"apt_list_upgradable_rc_{p.returncode}")
            return cnt, errors
        return cnt, errors
    except Exception as e:
        errors.append(f"apt_list_upgradable_exception:{e}")

    # Fallback path.
    try:
        p2 = _run(["apt-get", "-s", "upgrade"], timeout_sec=timeout_sec)
        cnt = 0
        for line in (p2.stdout or "").splitlines():
            if line.startswith("Inst "):
                cnt += 1
        if p2.returncode not in (0, 100):
            errors.append(f"apt_get_sim_upgrade_rc_{p2.returncode}")
        return cnt, errors
    except Exception as e:
        errors.append(f"apt_get_sim_upgrade_exception:{e}")
        return 0, errors


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


def collect_apt_state(*, timeout_sec: int = 45) -> dict[str, Any]:
    pending, pending_err = _pending_updates(timeout_sec=max(10, int(timeout_sec)))
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

    level = 0 if status == "ok" else 5
    return {
        "status": status,
        "level": level,
        "pending_updates": int(pending),
        "error_count": int(len(errors)),
        "errors": errors[:20],
        "duplicate_sources": duplicates,
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def apply_apt_profile(profile: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    if profile is None:
        profile = {}
    if dry_run:
        current = collect_apt_state(timeout_sec=25)
        return {
            "status": "planned",
            "actions": [
                "remove_third_party_sources",
                "dedupe_list_sources",
                "repair_repos_on_error",
                "apt_update",
                "optional_package_ops",
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

    state = collect_apt_state(timeout_sec=45)
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
