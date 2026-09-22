from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable
from urllib.parse import urlsplit


SCHEMA = "mcd-mautic-assetmapper-verification-v1"
_CONTENT_TYPES = {
    ".css": {"text/css"},
    ".js": {"application/javascript", "text/javascript", "application/x-javascript"},
}


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _configured_webroots(project_root: Path) -> list[Path]:
    composer = _json_object(project_root / "composer.json")
    extra = composer.get("extra") if isinstance(composer.get("extra"), dict) else {}
    configured: list[str] = []
    scaffold = extra.get("mautic-scaffold") if isinstance(extra.get("mautic-scaffold"), dict) else {}
    locations = scaffold.get("locations") if isinstance(scaffold.get("locations"), dict) else {}
    for value in (locations.get("web-root"), extra.get("public-dir")):
        if isinstance(value, str) and value.strip():
            configured.append(value.strip().rstrip("/"))
    roots: list[Path] = []
    for value in configured:
        candidate = (project_root / value).resolve() if value != "." else project_root.resolve()
        if candidate not in roots:
            roots.append(candidate)
    return roots


def discover_asset_webroot(project_root: str | Path, install_root: str | Path) -> Path | None:
    """Resolve the actual served root from Mautic config and discovered install state."""
    project = Path(project_root).resolve()
    install = Path(install_root).resolve()
    candidates = _configured_webroots(project) + [install, project]
    for child in sorted(project.iterdir(), key=lambda item: item.name) if project.is_dir() else []:
        if child.is_dir() and (child / "index.php").is_file():
            candidates.append(child)
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "index.php").is_file():
            return candidate
    return None


def _manifest_asset_paths(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_manifest_asset_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_manifest_asset_paths(item))
    elif isinstance(value, str):
        raw = value.strip()
        path = urlsplit(raw).path
        if Path(path).suffix.lower() in _CONTENT_TYPES and path not in found:
            found.append(path)
    return found


def _asset_url_path(raw: str) -> str | None:
    path = urlsplit(raw).path
    if not path or not path.startswith("/"):
        path = "/" + path
    if ".." in Path(path).parts:
        return None
    return path


def _header_value(headers: str, name: str) -> str:
    values = []
    for line in headers.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            values.append(line.split(":", 1)[1].strip().lower().split(";", 1)[0])
    return values[-1] if values else ""


def _status_code(headers: str) -> int | None:
    matches = re.findall(r"^HTTP/\S+\s+(\d{3})\b", headers, flags=re.MULTILINE)
    return int(matches[-1]) if matches else None


def _run_runtime_command(
    command: list[str],
    *,
    cwd: Path,
    runtime_user: str,
    timeout_sec: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    user = str(runtime_user or "").strip()
    if not user or user == "root":
        raise ValueError("AssetMapper operations require a non-root instance runtime user")
    full = ["sudo", "-H", "-u", user, *command]
    return runner(full, cwd=str(cwd), text=True, capture_output=True, timeout=timeout_sec, check=False)


def verify_assetmapper_upgrade(
    *,
    project_root: str | Path,
    install_root: str | Path,
    console_path: str | Path,
    php_bin: str,
    runtime_user: str,
    domain: str,
    target_version: str,
    timeout_sec: int = 120,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Generate and verify public AssetMapper output without exposing command output."""
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "failed",
        "rollback_required": True,
        "project_root": str(Path(project_root).resolve()),
        "install_root": str(Path(install_root).resolve()),
        "target_version": str(target_version),
        "runtime_user": str(runtime_user),
        "commands": [],
        "assets": [],
    }
    project = Path(project_root).resolve()
    webroot = discover_asset_webroot(project, install_root)
    if webroot is None:
        result["reason"] = "served webroot could not be discovered"
        return result
    result["webroot"] = str(webroot)
    if not str(domain or "").strip():
        result["reason"] = "instance domain is unavailable for SNI/Host verification"
        return result
    console = str(Path(console_path).resolve())
    operations = [
        ("mautic:assets:generate", [str(php_bin), console, "mautic:assets:generate", "--no-interaction"]),
        ("cache:clear", [str(php_bin), console, "cache:clear", "--no-interaction"]),
    ]
    for name, command in operations:
        try:
            proc = _run_runtime_command(command, cwd=project, runtime_user=runtime_user, timeout_sec=timeout_sec, runner=runner)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result["reason"] = f"{name} could not run as runtime user: {type(exc).__name__}"
            return result
        result["commands"].append({"name": name, "returncode": proc.returncode, "runtime_user": runtime_user})
        if proc.returncode != 0:
            result["reason"] = f"{name} failed"
            return result

    manifest = webroot / "assets" / "build" / "manifest.json"
    result["manifest_path"] = str(manifest)
    result["manifest_url"] = "/assets/build/manifest.json"
    if not manifest.is_file():
        result["reason"] = "AssetMapper manifest is missing from the served webroot"
        return result
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["reason"] = "AssetMapper manifest is not valid JSON"
        return result
    raw_assets = _manifest_asset_paths(data)
    if not raw_assets:
        result["reason"] = "AssetMapper manifest contains no CSS or JavaScript references"
        return result

    for raw in raw_assets:
        url_path = _asset_url_path(raw)
        suffix = Path(url_path or raw).suffix.lower()
        asset = {"manifest_value": raw, "url": url_path, "suffix": suffix}
        if url_path is None or suffix not in _CONTENT_TYPES:
            asset["status"] = "failed"
            asset["reason"] = "unsafe or unsupported manifest asset path"
            result["assets"].append(asset)
            result["reason"] = "manifest references an unsafe or unsupported asset"
            return result
        local = (webroot / url_path.lstrip("/")).resolve()
        asset["path"] = str(local)
        try:
            local.relative_to(webroot)
        except ValueError:
            asset["status"] = "failed"
            asset["reason"] = "asset escapes served webroot"
            result["assets"].append(asset)
            result["reason"] = "manifest asset escapes served webroot"
            return result
        asset["exists"] = local.is_file()
        if not local.is_file():
            asset["status"] = "failed"
            result["assets"].append(asset)
            result["reason"] = "manifest-referenced asset is missing from served webroot"
            return result
        url = f"https://{domain.strip()}{url_path}"
        try:
            proc = runner(
                ["curl", "-ksS", "--max-time", str(max(5, int(timeout_sec))), "--resolve", f"{domain.strip()}:443:127.0.0.1", "-o", "/dev/null", "-D", "-", url],
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            asset["status"] = "failed"
            asset["reason"] = f"HTTP probe failed: {type(exc).__name__}"
            result["assets"].append(asset)
            result["reason"] = "manifest asset HTTP verification failed"
            return result
        headers = proc.stdout or proc.stderr or ""
        status = _status_code(headers)
        content_type = _header_value(headers, "content-type")
        asset.update({"http_status": status, "content_type": content_type, "status": "ok"})
        result["assets"].append(asset)
        if proc.returncode != 0 or status != 200 or content_type not in _CONTENT_TYPES[suffix]:
            asset["status"] = "failed"
            result["reason"] = "manifest asset did not return HTTP 200 with the expected content type"
            return result
    result["status"] = "success"
    result["rollback_required"] = False
    result["asset_count"] = len(result["assets"])
    return result
