from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any


NGINX_CONF = Path("/etc/nginx/nginx.conf")
SITES_AVAILABLE = Path("/etc/nginx/sites-available")
SITES_ENABLED = Path("/etc/nginx/sites-enabled")
BACKUP_ROOT = Path("/var/backups/mcd-nginx-baseline")


@dataclass
class _Snapshot:
    path: Path
    kind: str
    backup: Path | None = None
    target: str | None = None


def _nginx_present() -> bool:
    return bool(shutil.which("nginx") or Path("/usr/sbin/nginx").exists() or NGINX_CONF.exists())


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _has_www_data_user(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^user\s+www-data\s*;", line):
            return True
        if re.match(r"^user\s+\S+\s*;", line):
            return False
    return False


def _has_sites_enabled_include(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^include\s+/etc/nginx/sites-enabled/[^;]*;", line):
            return True
    return False


def _sites_enabled_has_regular_files() -> bool:
    if not SITES_ENABLED.exists():
        return False
    try:
        for path in SITES_ENABLED.iterdir():
            if path.name.startswith("."):
                continue
            if path.is_symlink():
                continue
            if path.is_file():
                return True
    except Exception:
        return True
    return False


def nginx_baseline_satisfied() -> bool:
    """Cheap read-only check for the SalesSnap nginx layout baseline."""
    if not _nginx_present():
        return True
    if NGINX_CONF.exists():
        text = _read_text(NGINX_CONF)
        if not _has_www_data_user(text):
            return False
        if not _has_sites_enabled_include(text):
            return False
    if _sites_enabled_has_regular_files():
        return False
    return True


def _snapshot(path: Path, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> None:
    path = Path(path)
    if path in snapshots:
        return
    if path.is_symlink():
        snapshots[path] = _Snapshot(path=path, kind="symlink", target=os.readlink(path))
        return
    if os.path.exists(path):
        rel = str(path).lstrip("/").replace("/", "__")
        backup = backup_dir / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        snapshots[path] = _Snapshot(path=path, kind="file", backup=backup)
        return
    snapshots[path] = _Snapshot(path=path, kind="missing")


def _restore_snapshots(snapshots: dict[Path, _Snapshot]) -> None:
    for snap in reversed(list(snapshots.values())):
        path = snap.path
        try:
            if os.path.lexists(path):
                path.unlink()
            if snap.kind == "file" and snap.backup is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap.backup, path)
            elif snap.kind == "symlink" and snap.target is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(snap.target, path)
        except Exception:
            continue


def _desired_nginx_conf(text: str) -> tuple[str, list[str]]:
    actions: list[str] = []
    lines = text.splitlines()
    out: list[str] = []
    user_seen = False
    user_changed = False
    for raw in lines:
        line = raw.strip()
        if not line.startswith("#") and re.match(r"^user\s+\S+\s*;", line):
            if not user_seen:
                indent = raw[: len(raw) - len(raw.lstrip())]
                desired = f"{indent}user www-data;"
                out.append(desired)
                user_seen = True
                if raw != desired:
                    user_changed = True
            else:
                user_changed = True
            continue
        out.append(raw)
    if not user_seen:
        out.insert(0, "user www-data;")
        user_changed = True
    if user_changed:
        actions.append("user_www_data")

    text_after_user = "\n".join(out)
    if _has_sites_enabled_include(text_after_user):
        return text_after_user.rstrip("\n") + "\n", actions

    inserted = False
    final: list[str] = []
    for raw in out:
        final.append(raw)
        if not inserted and re.match(r"^\s*http\s*\{", raw):
            indent = raw[: len(raw) - len(raw.lstrip())] + "    "
            final.append(f"{indent}include /etc/nginx/sites-enabled/*.conf;")
            inserted = True
    if inserted:
        actions.append("sites_enabled_include")
    return "\n".join(final).rstrip("\n") + "\n", actions


def _convert_sites_enabled_regular_files(backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    actions: list[str] = []
    if not SITES_ENABLED.exists():
        return actions
    for enabled in sorted(SITES_ENABLED.iterdir()):
        if enabled.name.startswith("."):
            continue
        if enabled.is_symlink():
            continue
        if not enabled.is_file():
            continue
        SITES_AVAILABLE.mkdir(parents=True, exist_ok=True)
        available = SITES_AVAILABLE / enabled.name
        _snapshot(enabled, backup_dir, snapshots)
        _snapshot(available, backup_dir, snapshots)
        try:
            copy_required = True
            if available.exists() and available.is_file():
                try:
                    copy_required = enabled.read_bytes() != available.read_bytes()
                except Exception:
                    copy_required = True
            if copy_required:
                shutil.copy2(enabled, available)
                actions.append(f"sites_available_updated:{available.name}")
            enabled.unlink()
            os.symlink(str(available), str(enabled))
            actions.append(f"sites_enabled_symlink:{enabled.name}")
        except Exception as e:
            actions.append(f"sites_enabled_symlink_failed:{enabled.name}:{e}")
    return actions


def _run(cmd: list[str], timeout_sec: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, int(timeout_sec)))


def _nginx_test() -> tuple[bool, str]:
    if not shutil.which("nginx") and not Path("/usr/sbin/nginx").exists():
        return True, "nginx_test:skipped_no_binary"
    proc = _run(["nginx", "-t"], timeout_sec=30)
    msg = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode == 0:
        return True, "nginx_test:ok"
    return False, msg or "nginx -t failed"


def _reload_nginx() -> tuple[bool, str]:
    if shutil.which("systemctl"):
        active = _run(["systemctl", "is-active", "nginx"], timeout_sec=10)
        if active.returncode != 0 or (active.stdout or "").strip() != "active":
            return True, "nginx_reload:skipped_inactive"
        proc = _run(["systemctl", "reload", "nginx"], timeout_sec=30)
        msg = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode == 0:
            return True, "nginx_reload:ok"
        return False, msg or "nginx reload failed"
    if shutil.which("service"):
        proc = _run(["service", "nginx", "reload"], timeout_sec=30)
        msg = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode == 0:
            return True, "nginx_reload:ok"
        return False, msg or "nginx reload failed"
    return True, "nginx_reload:skipped_no_service_manager"


def ensure_nginx_baseline(*, reload_service: bool = True) -> dict[str, Any]:
    """Converge nginx to SalesSnap runtime assumptions safely and idempotently."""
    actions: list[str] = []
    if not _nginx_present():
        return {"status": "skipped", "changed": False, "actions": ["nginx:absent"]}

    backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d%H%M%S")
    snapshots: dict[Path, _Snapshot] = {}
    changed = False

    try:
        if NGINX_CONF.exists():
            original = _read_text(NGINX_CONF)
            desired, conf_actions = _desired_nginx_conf(original)
            if desired != original:
                backup_dir.mkdir(parents=True, exist_ok=True)
                _snapshot(NGINX_CONF, backup_dir, snapshots)
                NGINX_CONF.write_text(desired, encoding="utf-8")
                actions.extend(conf_actions)
                changed = True
        if SITES_ENABLED.exists():
            symlink_actions = _convert_sites_enabled_regular_files(backup_dir, snapshots)
            if symlink_actions:
                actions.extend(symlink_actions)
                changed = True

        if not changed:
            return {"status": "ok", "changed": False, "actions": ["nginx_baseline:already_ok"]}

        test_ok, test_msg = _nginx_test()
        actions.append(test_msg if test_ok else "nginx_test:failed")
        if not test_ok:
            _restore_snapshots(snapshots)
            return {
                "status": "error",
                "changed": False,
                "actions": actions + ["rollback:done"],
                "error": test_msg,
            }

        if reload_service:
            reload_ok, reload_msg = _reload_nginx()
            actions.append(reload_msg if reload_ok else "nginx_reload:failed")
            if not reload_ok:
                return {"status": "error", "changed": True, "actions": actions, "error": reload_msg}

        return {"status": "ok", "changed": True, "actions": actions}
    except Exception as e:
        if snapshots:
            _restore_snapshots(snapshots)
        return {"status": "error", "changed": False, "actions": actions + ["rollback:done"], "error": str(e)}
