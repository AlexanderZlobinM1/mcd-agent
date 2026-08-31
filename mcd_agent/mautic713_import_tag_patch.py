from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from mcd_agent.mautic6_core_patch import detect_mautic_version
from mcd_agent.models import MauticInstall


PATCH_VERSION = "mautic-7.1.3-import-tag-detach-v1"
_TARGET_VERSION = "7.1.3"
_REL_PATHS = (
    "app/bundles/LeadBundle/Model/LeadModel.php",
    "docroot/app/bundles/LeadBundle/Model/LeadModel.php",
    "public/app/bundles/LeadBundle/Model/LeadModel.php",
)
_ORIGINAL = "$tagToBeAdded = $foundTags[$tag];"
_PATCHED = "$tagToBeAdded = $this->em->getReference(Tag::class, $foundTags[$tag]->getId());"
_MARKER = "MCD import tag detach remediation"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate(root: str) -> Path | None:
    base = Path(root)
    for rel in _REL_PATHS:
        path = base / rel
        if path.is_file():
            return path
    return None


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".mcd-import-tag-713.bak")


def _metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".mcd-import-tag-713.json")


def _write(path: Path, value: str, stat: os.stat_result) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, stat.st_mode)
    try:
        os.chown(temporary, stat.st_uid, stat.st_gid)
    except PermissionError:
        pass
    os.replace(temporary, path)


def _gate(install: MauticInstall) -> tuple[str | None, dict[str, Any] | None]:
    version = detect_mautic_version(install)
    if version != _TARGET_VERSION:
        return version, {"status": "skip", "reason": "version_not_7_1_3", "version": version, "root": install.root}
    return version, None


def patch_status(install: MauticInstall) -> dict[str, Any]:
    version, skipped = _gate(install)
    if skipped:
        return skipped
    path = _candidate(install.root)
    if path is None:
        return {"status": "skip", "reason": "lead_model_not_found", "version": version, "root": install.root}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"status": "error", "reason": f"read_failed: {exc}", "path": str(path), "root": install.root}
    common = {"path": str(path), "backup": str(_backup_path(path)), "metadata": str(_metadata_path(path)), "version": version, "root": install.root}
    if _PATCHED in text:
        return {"status": "patched", **common}
    if _ORIGINAL in text:
        return {"status": "vulnerable", **common}
    return {"status": "skip", "reason": "pattern_not_found", **common}


def ensure_patch(install: MauticInstall) -> dict[str, Any]:
    status = patch_status(install)
    if status.get("status") != "vulnerable":
        if status.get("status") == "patched":
            status["status"] = "already"
        return status
    path = Path(str(status["path"]))
    try:
        original = path.read_text(encoding="utf-8", errors="ignore")
        patched = original.replace(_ORIGINAL, f"// {_MARKER} ({PATCH_VERSION})\n                    {_PATCHED}", 1)
        if patched == original:
            return {"status": "error", "reason": "replacement_failed", **status}
        st = path.stat()
        backup = _backup_path(path)
        if not backup.exists():
            _write(backup, original, st)
        metadata = {
            "patch": PATCH_VERSION,
            "version": _TARGET_VERSION,
            "path": str(path),
            "original_sha256": _sha256(original),
            "patched_sha256": _sha256(patched),
        }
        _write(_metadata_path(path), json.dumps(metadata, sort_keys=True) + "\n", st)
        _write(path, patched, st)
    except OSError as exc:
        return {"status": "error", "reason": f"write_failed: {exc}", **status}
    return {**status, "status": "patched"}


def revert_patch(install: MauticInstall, *, allow_other_version: bool = False) -> dict[str, Any]:
    version = detect_mautic_version(install)
    if version != _TARGET_VERSION and not allow_other_version:
        return {"status": "skip", "reason": "version_not_7_1_3", "version": version, "root": install.root}
    path = _candidate(install.root)
    if path is None:
        return {"status": "skip", "reason": "lead_model_not_found", "version": version, "root": install.root}
    backup = _backup_path(path)
    metadata_path = _metadata_path(path)
    try:
        current = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"status": "error", "reason": f"read_failed: {exc}", "path": str(path), "root": install.root}
    if _PATCHED not in current:
        return {"status": "clean", "reason": "patch_not_present", "path": str(path), "version": version, "root": install.root}
    if not backup.is_file() or not metadata_path.is_file():
        return {"status": "error", "reason": "backup_or_metadata_missing", "path": str(path), "root": install.root}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        original = backup.read_text(encoding="utf-8", errors="ignore")
        if metadata.get("patch") != PATCH_VERSION or metadata.get("original_sha256") != _sha256(original):
            return {"status": "error", "reason": "backup_metadata_mismatch", "path": str(path), "root": install.root}
        if metadata.get("patched_sha256") != _sha256(current):
            return {"status": "error", "reason": "patched_file_changed", "path": str(path), "root": install.root}
        _write(path, original, path.stat())
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "error", "reason": f"revert_failed: {exc}", "path": str(path), "root": install.root}
    return {"status": "reverted", "path": str(path), "backup": str(backup), "version": version, "root": install.root}
