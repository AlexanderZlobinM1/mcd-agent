from __future__ import annotations

import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.db import MauticDB
from mcd_agent.discovery import discover_mautic
from mcd_agent.executor import execute_mautic_command_template
from mcd_agent.runtime_overrides import fetch_runtime_overrides


_C_RESET = "\033[0m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_GRAY = "\033[90m"
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*Bundle(?:Dev)?$")
_EXCLUSIVE_BUNDLE_PAIRS: dict[str, str] = {
    "AmazonSesBundle": "AmazonSesBundleDev",
    "AmazonSesBundleDev": "AmazonSesBundle",
    "SalesSnapBundle": "SalesSnapBundleDev",
    "SalesSnapBundleDev": "SalesSnapBundle",
}


def _is_valid_bundle_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if n.lower() in {"plugin", "plugins"}:
        return False
    return bool(_BUNDLE_NAME_RE.match(n))


def _install_bundle_for_manifest_bundle(bundle: str) -> str:
    """
    Resolve filesystem install directory for a manifest bundle key.
    Dev aliases are installed into canonical bundle directory so runtime
    bundle paths remain stable.
    """
    b = str(bundle or "").strip()
    if b in _EXCLUSIVE_BUNDLE_PAIRS and b.endswith("Dev"):
        return _EXCLUSIVE_BUNDLE_PAIRS[b]
    return b


def _color(status: str, no_color: bool) -> str:
    if no_color:
        return status
    if status == "OK":
        return f"{_C_GREEN}{status}{_C_RESET}"
    if status == "UPDATE":
        return f"{_C_YELLOW}{status}{_C_RESET}"
    if status == "MISSING":
        return f"{_C_GRAY}{status}{_C_RESET}"
    if status == "BROKEN":
        return f"{_C_RED}{status}{_C_RESET}"
    return status


def _status_cell(status: str, no_color: bool, width: int = 7) -> str:
    colored = _color(status, no_color)
    pad = " " * max(0, width - len(status))
    return f"{colored}{pad}"


def _select_install(config: AgentConfig, root: str | None) -> tuple[str, int]:
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst.root, inst.mautic_major or 6
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    inst = installs[0]
    return inst.root, inst.mautic_major or 6


def _select_install_with_db(config: AgentConfig, root: str | None):
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    return installs[0]


def _repo_base_url(config: AgentConfig) -> str:
    if config.plugins_repo_base_url:
        return config.plugins_repo_base_url.rstrip("/")
    if config.mcc_url:
        return config.mcc_url.rstrip("/")
    raise RuntimeError("plugins.repo_base_url or mcc.url must be configured")


_DNS_OVERRIDE_LOCK = threading.Lock()


def _is_ip_literal(host: str | None) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _url_host(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    return parsed.hostname


def _plugins_fallback_ip(config: AgentConfig) -> str | None:
    local = str(config.plugins_repo_fallback_ip or "").strip()
    if local:
        return local
    if not (config.mcc_url and config.mcc_token):
        return None
    try:
        fetched = fetch_runtime_overrides(config)
        if str(fetched.get("status", "")).strip().lower() != "ok":
            return None
        runtime = fetched.get("runtime_overrides")
        if not isinstance(runtime, dict):
            return None
        remote = str(runtime.get("plugins_repo_fallback_ip", "")).strip()
        return remote or None
    except Exception:
        return None


def _urlopen_with_dns_override(
    req: urllib.request.Request,
    *,
    timeout_sec: int,
    resolve_host: str,
    resolve_ip: str,
):
    orig_getaddrinfo = socket.getaddrinfo
    resolve_host_l = resolve_host.lower()

    def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # type: ignore[no-untyped-def]
        h = str(host or "")
        if h.lower() == resolve_host_l:
            h = resolve_ip
        return orig_getaddrinfo(h, port, family, type, proto, flags)

    with _DNS_OVERRIDE_LOCK:
        socket.getaddrinfo = _patched_getaddrinfo
        try:
            return urllib.request.urlopen(req, timeout=timeout_sec)
        finally:
            socket.getaddrinfo = orig_getaddrinfo


def _fetch_json(
    url: str,
    token: str | None,
    *,
    timeout_sec: int = 12,
    fallback_ip: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": f"mcd-agent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    primary_err: Exception | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        primary_err = e

    host = _url_host(url)
    fallback_ip_clean = str(fallback_ip or "").strip()
    if not fallback_ip_clean or not host or _is_ip_literal(host):
        assert primary_err is not None
        raise primary_err

    logging.warning("plugins manifest primary fetch failed (%s), fallback via %s -> %s", primary_err, host, fallback_ip_clean)
    try:
        with _urlopen_with_dns_override(
            req,
            timeout_sec=timeout_sec,
            resolve_host=host,
            resolve_ip=fallback_ip_clean,
        ) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except (urllib.error.HTTPError, urllib.error.URLError):
        assert primary_err is not None
        raise primary_err


def _fetch_file(url: str, token: str | None, dst: Path, *, fallback_ip: str | None = None) -> None:
    headers: dict[str, str] = {"User-Agent": f"mcd-agent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    primary_err: Exception | None = None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, dst.open("wb") as f:
            shutil.copyfileobj(resp, f)
        return
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        primary_err = e

    host = _url_host(url)
    fallback_ip_clean = str(fallback_ip or "").strip()
    if not fallback_ip_clean or not host or _is_ip_literal(host):
        assert primary_err is not None
        raise primary_err

    logging.warning("plugins package primary fetch failed (%s), fallback via %s -> %s", primary_err, host, fallback_ip_clean)
    try:
        with _urlopen_with_dns_override(
            req,
            timeout_sec=120,
            resolve_host=host,
            resolve_ip=fallback_ip_clean,
        ) as resp, dst.open("wb") as f:
            shutil.copyfileobj(resp, f)
    except (urllib.error.HTTPError, urllib.error.URLError):
        assert primary_err is not None
        raise primary_err


def _parse_selection(expr: str, max_index: int) -> list[int]:
    out: set[int] = set()
    parts = [x.strip() for x in expr.split() if x.strip()]
    for part in parts:
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start = int(a)
                end = int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for idx in range(start, end + 1):
                if 1 <= idx <= max_index:
                    out.add(idx)
            continue
        try:
            idx = int(part)
        except ValueError:
            continue
        if 1 <= idx <= max_index:
            out.add(idx)
    return sorted(out)


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _normalize_action(raw: str) -> str | None:
    v = raw.strip().lower()
    if not v:
        return "auto"
    mapping = {
        "a": "auto",
        "auto": "auto",
        "i": "install",
        "install": "install",
        "u": "update",
        "update": "update",
        "r": "reinstall",
        "reinstall": "reinstall",
        "d": "remove",
        "remove": "remove",
        "x": "exit",
        "q": "exit",
        "exit": "exit",
    }
    return mapping.get(v)


def _exclusive_counterparts(bundle: str, item: dict[str, Any] | None = None) -> set[str]:
    """
    Returns bundles that are mutually exclusive with `bundle`.
    Supports hardcoded pairs and optional manifest field `replaces: []`.
    """
    out: set[str] = set()
    bundle_name = str(bundle or "").strip()
    if not bundle_name:
        return out

    direct = _EXCLUSIVE_BUNDLE_PAIRS.get(bundle_name)
    if direct:
        out.add(direct)
    for left, right in _EXCLUSIVE_BUNDLE_PAIRS.items():
        if right == bundle_name:
            out.add(left)

    if isinstance(item, dict):
        repl = item.get("replaces")
        if isinstance(repl, list):
            for x in repl:
                name = str(x or "").strip()
                if name and _is_valid_bundle_name(name):
                    out.add(name)

    out.discard(bundle_name)
    return out


def _validate_selected_exclusive_conflicts(selected: list[dict[str, Any]]) -> None:
    selected_set = {str(row.get("bundle", "")).strip() for row in selected if str(row.get("bundle", "")).strip()}
    for row in selected:
        bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        conflicts = sorted(selected_set.intersection(_exclusive_counterparts(bundle, item_dict)))
        if conflicts:
            raise RuntimeError(
                f"exclusive plugins selected together: {bundle} and {', '.join(conflicts)}"
            )


def _auto_remove_conflicting_installed_bundles(
    selected: list[dict[str, Any]],
    plugins_dir: Path,
) -> list[str]:
    """
    Return conflicting bundle keys that must be removed before applying `selected`.
    We intentionally do this unconditionally (even if paths are currently absent)
    to enforce deterministic "one-of" behavior for dev/stable variants.
    """
    _ = plugins_dir  # kept for call-site compatibility
    selected_set = {str(row.get("bundle", "")).strip() for row in selected if str(row.get("bundle", "")).strip()}
    remove: set[str] = set()
    for row in selected:
        bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        for conflict in _exclusive_counterparts(bundle, item_dict):
            if conflict in selected_set:
                continue
            remove.add(conflict)
    return sorted(remove, key=lambda x: x.lower())


def _remove_plugin_path(path: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def _extract_version_from_php_text(text: str) -> str:
    m = re.search(r"['\"]version['\"]\s*=>\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"['\"]version['\"]\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "-"


def _read_installed_version(plugin_dir: Path) -> str:
    cfg = plugin_dir / "Config" / "config.php"
    if not cfg.exists():
        return "-"
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "-"
    return _extract_version_from_php_text(text)


def _resolve_plugins_dir(root: str, create: bool = False) -> Path:
    base = Path(root)
    candidates = [
        base / "plugins",
        base / "docroot" / "plugins",
        base / "public" / "plugins",
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    if create:
        candidates[0].mkdir(parents=True, exist_ok=True)
    return candidates[0]


def _has_php_file_fast(root: Path, limit_files: int = 50000) -> bool:
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Skip hidden dirs to avoid accidental deep scans.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            seen += 1
            if fn.endswith(".php"):
                return True
            if seen >= limit_files:
                return True
    return False


def _plugin_status(
    plugins_dir: Path,
    plugin: dict[str, Any],
    state_filename: str,
    *,
    install_bundle: str | None = None,
) -> tuple[str, str, str]:
    bundle = str(plugin.get("bundle", "")).strip()
    install_name = str(install_bundle or "").strip() or bundle
    pdir = plugins_dir / install_name
    if not pdir.exists():
        return "MISSING", "not installed", "-"

    required = plugin.get("required_files")
    if isinstance(required, list):
        for rel in required:
            relp = pdir / str(rel)
            if not relp.exists():
                return "BROKEN", f"missing {rel}", _read_installed_version(pdir)
    else:
        if not _has_php_file_fast(pdir):
            return "BROKEN", "no php files", _read_installed_version(pdir)

    installed_version = _read_installed_version(pdir)
    expected_version = str(plugin.get("version", "")).strip() or "-"
    expected_sha = str(plugin.get("sha256", "")).strip()

    state: dict[str, Any] | None = None
    state_path = pdir / state_filename
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return "BROKEN", "invalid state metadata", installed_version

    # Canonical-path dev/stable aliases share one install directory.
    # Respect the last installed bundle from state file so UI/CLI won't
    # show both variants as simultaneously installed.
    if state is not None:
        recorded_bundle = str(state.get("bundle", "")).strip()
        if recorded_bundle and recorded_bundle != bundle:
            if recorded_bundle in _exclusive_counterparts(bundle, plugin):
                return "MISSING", f"counterpart installed={recorded_bundle}", "-"

    if installed_version == expected_version:
        if state is None:
            return "OK", "version match", installed_version
        installed_sha = str(state.get("sha256", "")).strip()
        if expected_sha and installed_sha == expected_sha:
            return "OK", "version+sha match", installed_version
        return "OK", "version match", installed_version

    return "UPDATE", f"installed={installed_version}", installed_version


def _extract_package(archive_path: Path, staging: Path) -> None:
    lower = archive_path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(staging)
    else:
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(staging)
    _cleanup_macos_archive_artifacts(staging)


def _cleanup_macos_archive_artifacts(root: Path) -> None:
    """
    Remove macOS metadata artifacts (AppleDouble/._* and __MACOSX) that can
    introduce duplicate PHP classes and crash cache:clear.
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        cur = Path(dirpath)
        for d in list(dirnames):
            if d == "__MACOSX":
                try:
                    shutil.rmtree(cur / d, ignore_errors=True)
                except Exception:
                    pass
                dirnames.remove(d)
        for fn in filenames:
            if fn.startswith("._"):
                try:
                    (cur / fn).unlink(missing_ok=True)
                except Exception:
                    pass


def _find_bundle_root(staging: Path, bundle: str) -> Path:
    direct = staging / bundle
    if direct.exists() and direct.is_dir():
        return direct
    dirs = [x for x in staging.iterdir() if x.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    for d in dirs:
        if d.name.lower().startswith(bundle.lower()) or d.name.lower().endswith("-main"):
            return d
    raise RuntimeError(f"cannot locate bundle root for {bundle} in {staging}")


def _set_owner_group(path: Path, owner_group: str = "www-data:www-data") -> None:
    subprocess.run(["chown", "-R", owner_group, str(path)], check=True)


def _install_or_replace_plugin(
    *,
    root: str,
    bundle: str,
    package_url: str,
    token: str | None,
    fallback_ip: str | None,
    state_filename: str,
    state_payload: dict[str, Any],
) -> None:
    plugins_dir = _resolve_plugins_dir(root, create=True)
    dst_dir = plugins_dir / bundle
    ts = int(time.time())
    backup_dir = plugins_dir / f".{bundle}.bak-{ts}"

    with tempfile.TemporaryDirectory(prefix=f"mcd-plugin-{bundle}-") as td:
        td_path = Path(td)
        archive_path = td_path / package_url.rsplit("/", 1)[-1]
        _fetch_file(package_url, token, archive_path, fallback_ip=fallback_ip)

        unpack_dir = td_path / "unpack"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        _extract_package(archive_path, unpack_dir)
        source_dir = _find_bundle_root(unpack_dir, bundle)

        if dst_dir.exists():
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            dst_dir.rename(backup_dir)

        shutil.copytree(source_dir, dst_dir)
        (dst_dir / state_filename).write_text(json.dumps(state_payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        _set_owner_group(dst_dir)

        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)


def _run_post_steps(config: AgentConfig, install) -> None:
    root = install.root

    def _run_template(template: str) -> tuple[int, str]:
        return execute_mautic_command_template(
            php_bin=config.php_bin,
            run_as_user=config.mautic_run_as_user,
            root=root,
            template=template,
            timeout_sec=config.command_timeout_sec,
        )

    def _is_metadata_null_reload_error(out: str) -> bool:
        text = str(out or "").lower()
        return (
            "pluginevent" in text
            and "metadata" in text
            and "must be of type array" in text
            and "null given" in text
        )

    def _repair_plugin_metadata_null_once() -> bool:
        if not install.db:
            return False
        db = MauticDB(install.db)
        repaired_any = False
        # Mautic bug workaround: malformed/null plugin metadata may break
        # mautic:plugin:install|reload with PluginUpdateEvent metadata=null.
        sql_fixes = [
            "UPDATE {prefix}plugins SET metadata = '[]' WHERE metadata IS NULL OR metadata = ''",
            "UPDATE {prefix}plugins SET metadata = '[]' WHERE metadata IS NOT NULL AND metadata <> '' AND JSON_VALID(metadata) = 0",
        ]
        for sql in sql_fixes:
            try:
                affected = db.execute_sql_template(sql)
                repaired_any = repaired_any or (int(affected) > 0)
                logging.info("[%s] plugin metadata repair affected=%s sql=%s", root, affected, sql)
            except Exception as e:
                # Keep going: some engines/schemas may reject JSON_VALID.
                logging.warning("[%s] plugin metadata repair skipped for sql=%s: %s", root, sql, e)
        return repaired_any

    if config.plugins_post_cache_clear:
        rc, out = _run_template("cache:clear")
        if rc != 0:
            raise RuntimeError(f"cache:clear failed: {out}")
    if config.plugins_post_install:
        rc, out = _run_template("mautic:plugin:install")
        if rc == 0:
            return
        if _is_metadata_null_reload_error(out):
            repaired = _repair_plugin_metadata_null_once()
            if repaired:
                logging.info("[%s] retry mautic:plugin:install after metadata repair", root)
                rc2, out2 = _run_template("mautic:plugin:install")
                if rc2 == 0:
                    return
                raise RuntimeError(
                    "mautic:plugin:install failed after metadata repair: "
                    f"{out2}"
                )
        raise RuntimeError(f"mautic:plugin:install failed: {out}")


def _run_manifest_sql_fixes(install, selected_rows: list[dict[str, Any]]) -> None:
    if not install.db:
        return
    db = MauticDB(install.db)
    for row in selected_rows:
        item = row.get("item")
        if not isinstance(item, dict):
            continue
        pre_sql = item.get("pre_sql")
        if not isinstance(pre_sql, list):
            continue
        bundle = str(row.get("bundle", "")).strip() or "plugin"
        for raw in pre_sql:
            sql = str(raw).strip()
            if not sql:
                continue
            try:
                affected = db.execute_sql_template(sql)
                logging.info("[%s] pre_sql %s affected=%s", install.root, bundle, affected)
            except Exception as e:
                raise RuntimeError(f"pre_sql failed for {bundle}: {e}") from e


def _cleanup_conflicting_plugin_rows(install, selected_rows: list[dict[str, Any]]) -> None:
    if not install.db:
        return
    selected_set = {
        str(row.get("bundle", "")).strip()
        for row in selected_rows
        if str(row.get("bundle", "")).strip()
    }
    conflicts: set[str] = set()
    for row in selected_rows:
        bundle = str(row.get("bundle", "")).strip()
        item = row.get("item")
        item_dict = item if isinstance(item, dict) else None
        for other in _exclusive_counterparts(bundle, item_dict):
            if other and other not in selected_set:
                conflicts.add(other)
    if not conflicts:
        return
    escaped = []
    for b in sorted(conflicts):
        escaped.append("'" + b.replace("'", "''") + "'")
    sql = f"DELETE FROM {{prefix}}plugins WHERE bundle IN ({', '.join(escaped)})"
    try:
        db = MauticDB(install.db)
        affected = db.execute_sql_template(sql)
        logging.info("[%s] plugin conflict rows cleanup affected=%s bundles=%s", install.root, affected, ",".join(sorted(conflicts)))
    except Exception as e:
        logging.warning("[%s] plugin conflict rows cleanup failed: %s", install.root, e)


def _apply_hostnet_mautic4_tx_patch(install, selected_rows: list[dict[str, Any]]) -> None:
    if (install.mautic_major or 0) != 4:
        return

    engine_path = Path(install.root) / "app" / "bundles" / "IntegrationsBundle" / "Migration" / "Engine.php"
    if not engine_path.exists():
        logging.warning("[%s] m4 tx patch skipped: Engine.php not found", install.root)
    else:
        text = engine_path.read_text(encoding="utf-8", errors="ignore")
        if "isTransactionActive" not in text or "getTransactionNestingLevel" not in text:
            commit_re = re.compile(r"^([ \t]*)\$this->entityManager->commit\(\);\s*$", flags=re.MULTILINE)
            rollback_re = re.compile(r"^([ \t]*)\$this->entityManager->rollback\(\);\s*$", flags=re.MULTILINE)

            def _commit_guard(m: re.Match[str]) -> str:
                i = m.group(1)
                return (
                    f"{i}$conn = $this->entityManager->getConnection();\n"
                    f"{i}if ((method_exists($conn, 'isTransactionActive') && $conn->isTransactionActive()) || "
                    f"(method_exists($conn, 'getTransactionNestingLevel') && $conn->getTransactionNestingLevel() > 0)) {{\n"
                    f"{i}    $this->entityManager->commit();\n"
                    f"{i}}}"
                )

            def _rollback_guard(m: re.Match[str]) -> str:
                i = m.group(1)
                return (
                    f"{i}$conn = $this->entityManager->getConnection();\n"
                    f"{i}if ((method_exists($conn, 'isTransactionActive') && $conn->isTransactionActive()) || "
                    f"(method_exists($conn, 'getTransactionNestingLevel') && $conn->getTransactionNestingLevel() > 0)) {{\n"
                    f"{i}    $this->entityManager->rollback();\n"
                    f"{i}}}"
                )

            new_text, n_commit = commit_re.subn(_commit_guard, text, count=1)
            new_text, n_rollback = rollback_re.subn(_rollback_guard, new_text, count=1)
            if n_commit > 0 or n_rollback > 0:
                engine_path.write_text(new_text, encoding="utf-8")
                logging.info("[%s] m4 tx patch (Engine.php) applied: commit=%s rollback=%s", install.root, n_commit, n_rollback)

    hostnet_path = Path(install.root) / "plugins" / "HostnetAuthBundle" / "HostnetAuthBundle.php"
    if not hostnet_path.exists():
        hostnet_path = _resolve_plugins_dir(install.root, create=False) / "HostnetAuthBundle" / "HostnetAuthBundle.php"
    if not hostnet_path.exists():
        return
    hostnet_text = hostnet_path.read_text(encoding="utf-8", errors="ignore")
    if "$db->beginTransaction();" in hostnet_text:
        block_re = re.compile(
            r"if \(!empty\(\$queries\)\) \{\s*\$db->beginTransaction\(\);\s*try \{\s*foreach \(\$queries as \$q\) \{\s*\$db->query\(\$q\);\s*\}\s*.*?\s*\}\s*catch \(\\Exception \$e\) \{\s*.*?\s*throw \$e;\s*\}\s*\}",
            flags=re.DOTALL,
        )
        repl = (
            "if (!empty($queries)) {\n"
            "            foreach ($queries as $q) {\n"
            "                $db->query($q);\n"
            "            }\n"
            "        }"
        )
        hostnet_new, n_blocks = block_re.subn(repl, hostnet_text)
        if n_blocks > 0:
            hostnet_path.write_text(hostnet_new, encoding="utf-8")
            logging.info("[%s] m4 tx patch (HostnetAuthBundle.php) applied: blocks=%s", install.root, n_blocks)


def run_plugins_interactive(
    *,
    config: AgentConfig,
    root: str | None,
    selection: str | None,
    action: str | None,
    no_color: bool,
    yes: bool,
    list_available: bool = False,
    list_installed: bool = False,
) -> int:
    if (list_available or list_installed) and root is None:
        installs = discover_mautic(
            config.discovery_roots,
            config.exclude_path_contains,
            config.supported_mautic_majors,
            config.custom_instances,
        )
        if not installs:
            raise RuntimeError("No Mautic install found")
        rc = 0
        for inst in installs:
            print("")
            print(f"=== Instance: {inst.root} ===")
            try:
                run_plugins_interactive(
                    config=config,
                    root=inst.root,
                    selection=selection,
                    action=action,
                    no_color=no_color,
                    yes=yes,
                    list_available=list_available,
                    list_installed=list_installed,
                )
            except Exception as e:
                rc = 1
                print(f"Plugins list error for {inst.root}: {e}")
        return rc

    install = _select_install_with_db(config, root)
    install_root = install.root
    major = install.mautic_major or 6
    base = _repo_base_url(config)
    manifest_url = base + config.plugins_manifest_path_template.format(major=major)
    fallback_ip = _plugins_fallback_ip(config)
    logging.info("plugins manifest: %s", manifest_url)
    if fallback_ip:
        logging.info("plugins manifest fallback_ip: %s", fallback_ip)
    print("Loading plugin manifest...")
    try:
        manifest = _fetch_json(
            manifest_url,
            config.mcc_token,
            timeout_sec=12,
            fallback_ip=fallback_ip,
        )
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot fetch manifest (network/timeout): {e}") from e

    plugins = manifest.get("plugins", [])
    if not isinstance(plugins, list) or not plugins:
        print("No plugins in manifest")
        return 0

    plugins_dir = _resolve_plugins_dir(install_root, create=True)
    local_dirs = (
        sorted(
            [
                x
                for x in plugins_dir.iterdir()
                if x.is_dir() and not x.name.startswith(".") and _is_valid_bundle_name(x.name)
            ],
            key=lambda p: p.name.lower(),
        )
        if plugins_dir.exists()
        else []
    )
    manifest_dir = manifest_url.rsplit("/", 1)[0] + "/"

    manifest_by_bundle: dict[str, dict[str, Any]] = {}
    for item in plugins:
        if not isinstance(item, dict):
            continue
        bundle = str(item.get("bundle", "")).strip()
        if bundle and _is_valid_bundle_name(bundle):
            manifest_by_bundle[bundle] = item

    local_bundles = {d.name for d in local_dirs}
    all_bundles = sorted(set(local_bundles) | set(manifest_by_bundle.keys()), key=lambda x: x.lower())

    rows: list[dict[str, Any]] = []
    selectable_idx = 1
    for bundle in all_bundles:
        item = manifest_by_bundle.get(bundle)
        pdir = plugins_dir / bundle
        installed_version = _read_installed_version(pdir) if pdir.exists() else "-"
        if item is None:
            rows.append(
                {
                    "idx": selectable_idx,
                    "bundle": bundle,
                    "install_bundle": bundle,
                    "status": "-",
                    "reason": "local only (not in server manifest)",
                    "installed_version": installed_version,
                    "server_version": "-",
                    "package": "-",
                    "item": None,
                }
            )
            selectable_idx += 1
            continue

        install_bundle = _install_bundle_for_manifest_bundle(bundle)
        status, reason, installed_version = _plugin_status(
            plugins_dir,
            item,
            config.plugins_state_filename,
            install_bundle=install_bundle,
        )
        rows.append(
            {
                "idx": selectable_idx,
                "bundle": bundle,
                "install_bundle": install_bundle,
                "status": status,
                "reason": reason,
                "installed_version": installed_version,
                "server_version": str(item.get("version", "")).strip() or "-",
                "package": str(item.get("package", "")).strip(),
                "item": item,
            }
        )
        selectable_idx += 1

    if not rows:
        print("No plugin rows")
        return 0

    idx_map: dict[int, dict[str, Any]] = {}
    for row in rows:
        idx = row["idx"]
        if isinstance(idx, int):
            idx_map[idx] = row

    if list_available:
        print(f"Mautic root: {install_root}")
        print(f"Mautic major: {major}")
        print(f"Manifest: {manifest_url}")
        print("")
        print("Available plugins from server manifest:")
        print("Bundle                      Server       Package")
        print("--------------------------  -----------  ------------------------------")
        for row in rows:
            if row["item"] is None:
                continue
            print(f"{str(row['bundle']):<26}  {str(row['server_version']):<11}  {str(row['package'])}")
        return 0

    if list_installed:
        print(f"Mautic root: {install_root}")
        print(f"Mautic major: {major}")
        print(f"Manifest: {manifest_url}")
        print("")
        print("Installed plugins on host:")
        print("Bundle                      Installed")
        print("--------------------------  -----------")
        shown = 0
        for row in rows:
            if str(row["installed_version"]) == "-":
                continue
            print(f"{str(row['bundle']):<26}  {str(row['installed_version']):<11}")
            shown += 1
        if shown == 0:
            print("(none)")
        return 0

    print(f"Mautic root: {install_root}")
    print(f"Mautic major: {major}")
    print(f"Manifest: {manifest_url}")
    print("")
    print("Idx  Status   Bundle                      Installed    Server")
    print("---  -------  --------------------------  -----------  -----------")
    for row in rows:
        idx = row["idx"]
        idx_cell = f"{idx:>3}" if isinstance(idx, int) else "  -"
        print(
            f"{idx_cell}  {_status_cell(str(row['status']), no_color)}  "
            f"{str(row['bundle']):<26}  {str(row['installed_version']):<11}  {str(row['server_version']):<11}"
        )
    print("")

    selected: list[dict[str, Any]] = []
    auto_remove_bundles: list[str] = []
    while True:
        if action is None:
            print("Action:")
            print("a = auto, i = install, u = update, r = reinstall, d = remove, x = exit")
            action_in = _ask("Choose action [a/i/u/r/d/x], default=a: ")
        else:
            action_in = action
        normalized = _normalize_action(action_in)
        action = None
        if normalized is None:
            print("Invalid action, try again")
            continue
        if normalized == "exit":
            print("No changes")
            return 0
        chosen_action = normalized

        if selection is None:
            selection_in = _ask("Select plugins to apply (e.g. 1-3 6 10), empty=back: ").strip()
        else:
            selection_in = selection.strip()
            selection = None

        if not selection_in:
            if not sys.stdin.isatty():
                print("No selection provided in non-interactive mode, exit")
                return 0
            print("Back to action selection")
            continue

        indexes = _parse_selection(selection_in, max(idx_map.keys()) if idx_map else 0)
        if not indexes:
            print("No valid plugin indexes, try again")
            continue
        selected = [idx_map[i] for i in indexes if i in idx_map]
        if not selected:
            print("Selected indexes are not actionable, try again")
            continue

        try:
            _validate_selected_exclusive_conflicts(selected)
        except RuntimeError as e:
            print(f"Selection error: {e}")
            if not sys.stdin.isatty():
                return 1
            print("Back to action selection")
            continue

        auto_remove_bundles = []
        if chosen_action != "remove":
            auto_remove_bundles = _auto_remove_conflicting_installed_bundles(
                selected,
                plugins_dir,
            )

        print("Selected:")
        for row in selected:
            print(f"- {row['bundle']} [{row['status']}]")
        if auto_remove_bundles:
            print("Will auto-remove conflicting installed bundle(s):")
            for b in auto_remove_bundles:
                print(f"- {b}")
        if not yes:
            confirm = _ask("Apply selected plugins? [y/N]: ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("Cancelled, back to action selection")
                continue
        break

    action = chosen_action

    changed = False
    if action != "remove" and auto_remove_bundles:
        for conflict_bundle in auto_remove_bundles:
            conflict_install = _install_bundle_for_manifest_bundle(conflict_bundle)
            removed_paths: list[str] = []
            for name in sorted({conflict_bundle, conflict_install}):
                pdir = _resolve_plugins_dir(install_root, create=False) / name
                if _remove_plugin_path(pdir):
                    removed_paths.append(name)
            if removed_paths:
                changed = True
                logging.info(
                    "[%s] plugin %s auto-removed due to mutually exclusive selection (paths=%s)",
                    install_root,
                    conflict_bundle,
                    ",".join(removed_paths),
                )
                print(f"Auto-removed conflicting plugin: {conflict_bundle} ({', '.join(removed_paths)})")

    for row in selected:
        item = row["item"]
        bundle = row["bundle"]
        install_bundle = str(row.get("install_bundle") or _install_bundle_for_manifest_bundle(bundle))
        if action != "remove" and not isinstance(item, dict):
            logging.info("[%s] plugin %s not in server manifest, skip for action=%s", install_root, bundle, action)
            continue
        status = row["status"]
        if action == "remove":
            pdir = _resolve_plugins_dir(install_root, create=False) / install_bundle
            if pdir.exists():
                shutil.rmtree(pdir)
                changed = True
                logging.info("[%s] plugin %s removed (path=%s)", install_root, bundle, install_bundle)
            else:
                logging.info("[%s] plugin %s already absent (path=%s)", install_root, bundle, install_bundle)
            continue

        assert isinstance(item, dict)
        package = row["package"]
        package_url = str(item.get("url", "")).strip()
        if not package_url:
            package_url = urllib.parse.urljoin(manifest_dir, package)

        should_apply = False
        if action == "install":
            # Install is forceful: install/replace regardless of current state.
            should_apply = True
        elif action == "update":
            should_apply = status in {"UPDATE", "MISSING"}
        elif action == "reinstall":
            should_apply = True
        else:  # auto
            should_apply = status in {"MISSING", "UPDATE", "BROKEN"}

        if not should_apply:
            logging.info("[%s] plugin %s skip action=%s status=%s", install_root, bundle, action, status)
            continue

        _install_or_replace_plugin(
            root=install_root,
            bundle=install_bundle,
            package_url=package_url,
            token=config.mcc_token,
            fallback_ip=fallback_ip,
            state_filename=config.plugins_state_filename,
            state_payload={
                "bundle": bundle,
                "version": str(item.get("version", "")).strip(),
                "sha256": str(item.get("sha256", "")).strip(),
                "installed_at": datetime_now_iso(),
                "source": package_url,
            },
        )
        if install_bundle != bundle:
            alias_path = _resolve_plugins_dir(install_root, create=False) / bundle
            if alias_path.exists() or alias_path.is_symlink():
                try:
                    if alias_path.is_symlink() or alias_path.is_file():
                        alias_path.unlink()
                    elif alias_path.is_dir():
                        shutil.rmtree(alias_path)
                    logging.info("[%s] removed alias plugin path=%s (installed as %s)", install_root, bundle, install_bundle)
                except Exception as e:
                    logging.warning("[%s] failed to cleanup alias plugin path=%s: %s", install_root, bundle, e)
        changed = True
        logging.info("[%s] plugin %s applied action=%s path=%s", install_root, bundle, action, install_bundle)

    if changed:
        _run_manifest_sql_fixes(install, selected)
        _cleanup_conflicting_plugin_rows(install, selected)
        _apply_hostnet_mautic4_tx_patch(install, selected)
        _run_post_steps(config, install)
        print("Plugins applied and post-steps completed")
    else:
        print("No plugin changes required")
    return 0


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
