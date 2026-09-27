"""Normal runtime ownership preparation for Composer-managed destinations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import stat


def _inside(project: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(project):
        raise RuntimeError("Composer managed destination escapes the selected project")
    return resolved


def _managed_paths(project: Path, metadata_root: Path) -> list[Path]:
    manifest = json.loads((metadata_root / "composer.json").read_text(encoding="utf-8"))
    vendor = _inside(metadata_root, metadata_root / manifest.get("config", {}).get("vendor-dir", "vendor"))
    destinations = [project / vendor.relative_to(metadata_root)]
    installed = vendor / "composer" / "installed.json"
    if installed.is_file():
        value = json.loads(installed.read_text(encoding="utf-8"))
        packages = value.get("packages", []) if isinstance(value, dict) else value
        for package in packages:
            relative = package.get("install-path")
            if isinstance(relative, str) and relative:
                source = _inside(metadata_root, installed.parent / relative)
                destinations.append(project / source.relative_to(metadata_root))
    # Existing installer destinations also cover legacy/missing installed
    # metadata and parents of new packages without guessing package names.
    for template in manifest.get("extra", {}).get("installer-paths", {}):
        prefix = template.split("{", 1)[0].rstrip("/")
        if prefix:
            source = _inside(metadata_root, metadata_root / prefix)
            destinations.append(project / source.relative_to(metadata_root))
    return [_inside(project, path) for path in destinations]


def _align(path: Path, uid: int, gid: int, *, directory: bool) -> bool:
    current = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(current.st_mode):
        return False
    needed = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if directory else 0)
    mode = stat.S_IMODE(current.st_mode)
    changed = False
    if current.st_uid != uid:
        os.chown(path, uid, gid, follow_symlinks=False)
        changed = True
    if mode & needed != needed:
        os.chmod(path, mode | needed, follow_symlinks=False)
        changed = True
    return changed


def prepare_composer_paths(project_root: str, *, target_project_root: str | None = None,
                           runtime_user: str = "www-data") -> dict[str, int]:
    """Prepare only managed directories and Composer's two writable inputs.

    This is normal execution, not a read-only admission protocol. Native
    chown/chmod failures propagate as execution errors. No dependency source,
    upload, local configuration or symlink target is rewritten recursively.
    """
    project = Path(project_root).resolve(strict=True)
    account = pwd.getpwnam(runtime_user)
    roots = _managed_paths(project, project)
    if target_project_root:
        target = Path(target_project_root).resolve(strict=True)
        roots.extend(_managed_paths(project, target))
    directories = {project}
    for root in roots:
        parent = root
        while parent != project:
            if parent.is_dir() and not parent.is_symlink():
                directories.add(parent)
            parent = parent.parent
        if root.is_dir() and not root.is_symlink():
            def fail(error):
                raise error
            for directory, children, _files in os.walk(root, followlinks=False, onerror=fail):
                children[:] = [name for name in children if not (Path(directory) / name).is_symlink()]
                directories.add(Path(directory))
    repaired = sum(_align(path, account.pw_uid, account.pw_gid, directory=True)
                   for path in sorted(directories, key=lambda value: (len(value.parts), str(value))))
    inputs = 0
    for name in ("composer.json", "composer.lock"):
        path = project / name
        if path.is_file() and not path.is_symlink():
            inputs += _align(path, account.pw_uid, account.pw_gid, directory=False)
    return {"directories": len(directories), "repaired_directories": repaired, "repaired_inputs": inputs}
