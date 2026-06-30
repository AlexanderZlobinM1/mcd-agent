from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil
import subprocess
from typing import Any

from mcd_agent.models import MauticInstall


PHP_ETC_ROOT = Path("/etc/php")
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
GENERATED_ROOT = Path("/opt/mcd/generated")
BACKUP_ROOT = Path("/var/backups/mcd-instance-runtime")

_FASTCGI_SHARED_RE = re.compile(r"fastcgi_pass\s+unix:/(?:var/)?run/php/php(?P<version>\d+\.\d+)-fpm\.sock;")
_FASTCGI_MCD_RE = re.compile(r"fastcgi_pass\s+unix:/run/php/php(?P<version>\d+\.\d+)-fpm-mcd-[^;]+\.sock;")
_SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^;]+);", flags=re.MULTILINE)
_ROOT_RE = re.compile(r"^\s*root\s+([^;]+);", flags=re.MULTILINE)
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9_.-]+")
_NGINX_BACKUP_NAME_RE = re.compile(r"(?:^|[._-])(bak|backup|disabled|old|orig|save|tmp)(?:[._-]|$)", re.I)


@dataclass(slots=True)
class _Snapshot:
    path: Path
    existed: bool
    backup: Path | None = None
    symlink_target: str | None = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(cmd: list[str], *, timeout_sec: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)


def _safe_slug(value: str) -> str:
    base = str(value or "").strip().lower()
    if "://" in base:
        base = base.split("://", 1)[1]
    base = base.strip("/").split("/", 1)[0] or base
    base = _SAFE_SLUG_RE.sub("-", base).strip(".-")
    return base[:80] or "instance"


def _docroot_for_instance(inst: MauticInstall) -> Path:
    root = Path(inst.root)
    docroot = root / "docroot"
    if docroot.is_dir():
        return docroot
    return root


def _instance_domains(inst: MauticInstall) -> set[str]:
    out: set[str] = set()
    for raw in list(inst.domains or []) + [inst.primary_domain or "", inst.name or ""]:
        v = str(raw or "").strip().lower()
        if v and v not in {"_", "localhost"}:
            out.add(v)
    return out


def _pool_slug(inst: MauticInstall) -> str:
    domains = sorted(_instance_domains(inst))
    if domains:
        return _safe_slug(domains[0].split(".", 1)[0])
    return _safe_slug(inst.name or inst.instance_uid or Path(inst.root).name)


def _host_timezone() -> str:
    for p in (Path("/etc/timezone"),):
        try:
            val = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if val:
            return val
    proc = _run(["timedatectl", "show", "-p", "Timezone", "--value"], timeout_sec=5)
    val = (proc.stdout or "").strip()
    return val or "UTC"


def _instance_timezone(inst: MauticInstall) -> str:
    return str(inst.mautic_timezone or "").strip() or _host_timezone()


def _is_nginx_candidate(path: Path) -> bool:
    name = path.name
    if not name.endswith(".conf"):
        return False
    return not _NGINX_BACKUP_NAME_RE.search(name)


def _active_nginx_files() -> list[Path]:
    files: list[Path] = []
    if not NGINX_SITES_ENABLED.exists():
        return files
    for p in sorted(NGINX_SITES_ENABLED.iterdir()):
        if not _is_nginx_candidate(p):
            continue
        try:
            real = p.resolve(strict=True)
        except OSError:
            continue
        if not real.is_file() or not _is_nginx_candidate(real):
            continue
        if real not in files:
            files.append(real)
    return files


def _nginx_file_matches_instance(path: Path, inst: MauticInstall) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    docroot = str(_docroot_for_instance(inst))
    roots = [m.strip().strip('"\'') for m in _ROOT_RE.findall(text)]
    if docroot in roots:
        return True
    domains = _instance_domains(inst)
    if not domains:
        return False
    names: set[str] = set()
    for raw in _SERVER_NAME_RE.findall(text):
        names.update(x.strip().lower() for x in raw.split() if x.strip())
    return bool(domains & names)


def _php_versions_for_file(text: str) -> set[str]:
    out = {m.group("version") for m in _FASTCGI_SHARED_RE.finditer(text)}
    out.update(m.group("version") for m in _FASTCGI_MCD_RE.finditer(text))
    return {x for x in out if x}


def _wrapper_script(version: str, inst: MauticInstall, slug: str) -> str:
    tz = _instance_timezone(inst)
    php = shutil.which(f"php{version}") or f"/usr/bin/php{version}"
    return f"""#!/bin/sh
# Managed by MCD. Per-instance PHP CLI wrapper.
exec {php} \\
  -d date.timezone='{tz}' \\
  -d memory_limit='-1' \\
  -d max_execution_time='300' \\
  -d max_input_time='300' \\
  -d max_input_vars='5000' \\
  -d upload_max_filesize='64M' \\
  -d post_max_size='64M' \\
  "$@"
"""


def _write_if_changed(path: Path, text: str) -> bool:
    old = None
    if path.exists() and not path.is_symlink():
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = None
    if old == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _snapshot(path: Path, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> None:
    if path in snapshots:
        return
    snap = _Snapshot(path=path, existed=path.exists() or path.is_symlink())
    if snap.existed:
        backup = backup_dir / path.as_posix().lstrip("/")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            snap.symlink_target = os.readlink(path)
        elif path.is_dir():
            shutil.copytree(path, backup, symlinks=True, dirs_exist_ok=True)
            snap.backup = backup
        else:
            shutil.copy2(path, backup)
            snap.backup = backup
    snapshots[path] = snap


def _restore_snapshots(snapshots: dict[Path, _Snapshot]) -> None:
    for path, snap in reversed(list(snapshots.items())):
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        if not snap.existed:
            continue
        if snap.symlink_target is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(snap.symlink_target)
        elif snap.backup is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if snap.backup.is_dir():
                shutil.copytree(snap.backup, path, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(snap.backup, path)


def _unlink_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_file_if_exists(path: Path, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    _snapshot(path, backup_dir, snapshots)
    _unlink_path(path)
    return True


def _is_managed_include(path: Path) -> bool:
    if not path.exists() or path.is_symlink():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "Managed by MCD" in text and "pool.d/mcd/*.conf" in text


def _active_mcd_socket_versions() -> set[str]:
    versions: set[str] = set()
    for path in _active_nginx_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        versions.update(m.group("version") for m in _FASTCGI_MCD_RE.finditer(text))
    return {x for x in versions if x}


def _cleanup_fpm_include_if_unused(version: str, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> list[str]:
    actions: list[str] = []
    if version in _active_mcd_socket_versions():
        return actions
    pool_d = PHP_ETC_ROOT / version / "fpm" / "pool.d"
    include_file = pool_d / "99-mcd.conf"
    link = pool_d / "mcd"
    generated = GENERATED_ROOT / "php" / version / "fpm" / "pools"

    if _is_managed_include(include_file):
        if _remove_file_if_exists(include_file, backup_dir, snapshots):
            actions.append(f"fpm_include_removed:{version}")

    if link.exists() or link.is_symlink():
        if link.is_symlink() and os.readlink(link) == str(generated):
            if _remove_file_if_exists(link, backup_dir, snapshots):
                actions.append(f"fpm_link_removed:{version}")

    return actions


def _cleanup_instance_pool(version: str, slug: str, backup_dir: Path, snapshots: dict[Path, _Snapshot]) -> bool:
    pool_path = GENERATED_ROOT / "php" / version / "fpm" / "pools" / f"mcd-{slug}.conf"
    return _remove_file_if_exists(pool_path, backup_dir, snapshots)


def _rewrite_nginx_file_to_shared(path: Path, version: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    new = _FASTCGI_MCD_RE.sub(
        lambda m: (
            f"fastcgi_pass unix:/run/php/php{version}-fpm.sock;"
            if m.group("version") == version
            else m.group(0)
        ),
        text,
    )
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def _service_reload(service: str) -> tuple[bool, str]:
    active = _run(["systemctl", "is-active", service], timeout_sec=10)
    if active.returncode != 0 or (active.stdout or "").strip() != "active":
        return True, f"{service}:reload_skipped_inactive"
    proc = _run(["systemctl", "reload", service], timeout_sec=30)
    msg = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, f"{service}:reload_{'ok' if proc.returncode == 0 else 'failed'}{(': ' + msg) if msg else ''}"


def apply_instance_runtime(
    installs: list[MauticInstall],
    *,
    root: str | None = None,
    dry_run: bool = False,
    reload_services: bool = True,
) -> dict[str, Any]:
    selected = [
        inst
        for inst in installs
        if not root or root in {inst.root, inst.instance_uid, inst.name, inst.primary_domain or ""}
    ]
    backup_dir = BACKUP_ROOT / _utc_stamp()
    snapshots: dict[Path, _Snapshot] = {}
    actions: list[str] = []
    changed = False
    fpm_versions: set[str] = set()
    fpm_config_changed_versions: set[str] = set()
    results: list[dict[str, Any]] = []
    try:
        for inst in selected:
            slug = _pool_slug(inst)
            matched = [p for p in _active_nginx_files() if _nginx_file_matches_instance(p, inst)]
            inst_versions: set[str] = set()
            for path in matched:
                text = path.read_text(encoding="utf-8", errors="ignore")
                inst_versions.update(_php_versions_for_file(text))
            row: dict[str, Any] = {
                "root": inst.root,
                "name": inst.name,
                "slug": slug,
                "timezone": _instance_timezone(inst),
                "nginx_files": [str(p) for p in matched],
                "php_versions": sorted(inst_versions),
                "status": "ok" if inst_versions else "skipped",
                "reason": "" if inst_versions else "no_matching_php_fpm_vhost",
            }
            results.append(row)
            if dry_run:
                continue
            for version in sorted(inst_versions):
                fpm_versions.add(version)
                if _cleanup_instance_pool(version, slug, backup_dir, snapshots):
                    changed = True
                    fpm_config_changed_versions.add(version)
                    actions.append(f"pool_removed:{slug}:{version}")
                wrapper_dir = GENERATED_ROOT / "instances" / slug
                wrapper_path = wrapper_dir / "php"
                _snapshot(wrapper_path, backup_dir, snapshots)
                if _write_if_changed(wrapper_path, _wrapper_script(version, inst, slug)):
                    wrapper_path.chmod(0o755)
                    changed = True
                    actions.append(f"wrapper:{slug}:{version}")
                instance_wrapper = Path(inst.root) / ".mcd" / "php"
                _snapshot(instance_wrapper, backup_dir, snapshots)
                instance_wrapper.parent.mkdir(parents=True, exist_ok=True)
                if instance_wrapper.exists() or instance_wrapper.is_symlink():
                    if not instance_wrapper.is_symlink() or os.readlink(instance_wrapper) != str(wrapper_path):
                        instance_wrapper.unlink()
                        instance_wrapper.symlink_to(wrapper_path)
                        changed = True
                        actions.append(f"instance_wrapper:{slug}:{version}")
                else:
                    instance_wrapper.symlink_to(wrapper_path)
                    changed = True
                    actions.append(f"instance_wrapper:{slug}:{version}")
                for path in matched:
                    _snapshot(path, backup_dir, snapshots)
                    if _rewrite_nginx_file_to_shared(path, version):
                        changed = True
                        actions.append(f"nginx_shared:{path}:{version}")
                include_actions = _cleanup_fpm_include_if_unused(version, backup_dir, snapshots)
                if include_actions:
                    changed = True
                    fpm_config_changed_versions.add(version)
                    actions.extend(include_actions)
        if dry_run:
            return {"status": "ok", "changed": False, "dry_run": True, "instances": results, "actions": actions}
        for version in sorted(fpm_config_changed_versions):
            proc = _run([f"php-fpm{version}", "-t"], timeout_sec=30)
            if proc.returncode != 0:
                raise RuntimeError(f"php-fpm{version} -t failed: {(proc.stderr or proc.stdout or '').strip()}")
        if changed:
            proc = _run(["nginx", "-t"], timeout_sec=30)
            if proc.returncode != 0:
                raise RuntimeError(f"nginx -t failed: {(proc.stderr or proc.stdout or '').strip()}")
        reload_actions: list[str] = []
        if changed and reload_services:
            for version in sorted(fpm_config_changed_versions):
                ok, msg = _service_reload(f"php{version}-fpm")
                reload_actions.append(msg)
                if not ok:
                    raise RuntimeError(msg)
            ok, msg = _service_reload("nginx")
            reload_actions.append(msg)
            if not ok:
                raise RuntimeError(msg)
        return {
            "status": "ok",
            "changed": changed,
            "instances": results,
            "actions": actions,
            "reload": reload_actions,
            "backup_dir": str(backup_dir),
        }
    except Exception as e:
        if snapshots:
            _restore_snapshots(snapshots)
        return {
            "status": "error",
            "changed": False,
            "reason": str(e),
            "instances": results,
            "actions": actions,
            "backup_dir": str(backup_dir),
        }
