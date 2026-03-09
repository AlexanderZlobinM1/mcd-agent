from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
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


_C_RESET = "\033[0m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_GRAY = "\033[90m"
_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*Bundle$")


def _is_valid_bundle_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if n.lower() in {"plugin", "plugins"}:
        return False
    return bool(_BUNDLE_NAME_RE.match(n))


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


def _fetch_json(url: str, token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": f"mcd-agent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


def _fetch_file(url: str, token: str | None, dst: Path) -> None:
    headers: dict[str, str] = {"User-Agent": f"mcd-agent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)


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


def _plugin_status(plugins_dir: Path, plugin: dict[str, Any], state_filename: str) -> tuple[str, str, str]:
    bundle = str(plugin.get("bundle", "")).strip()
    pdir = plugins_dir / bundle
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
        return
    with tarfile.open(archive_path, "r:*") as tf:
        tf.extractall(staging)


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
        _fetch_file(package_url, token, archive_path)

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
    logging.info("plugins manifest: %s", manifest_url)
    print("Loading plugin manifest...")
    try:
        manifest = _fetch_json(manifest_url, config.mcc_token, timeout_sec=12)
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

        status, reason, installed_version = _plugin_status(plugins_dir, item, config.plugins_state_filename)
        rows.append(
            {
                "idx": selectable_idx,
                "bundle": bundle,
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

        print("Selected:")
        for row in selected:
            print(f"- {row['bundle']} [{row['status']}]")
        if not yes:
            confirm = _ask("Apply selected plugins? [y/N]: ").strip().lower()
            if confirm not in {"y", "yes"}:
                print("Cancelled, back to action selection")
                continue
        break

    action = chosen_action

    changed = False
    for row in selected:
        item = row["item"]
        bundle = row["bundle"]
        if action != "remove" and not isinstance(item, dict):
            logging.info("[%s] plugin %s not in server manifest, skip for action=%s", install_root, bundle, action)
            continue
        status = row["status"]
        if action == "remove":
            pdir = _resolve_plugins_dir(install_root, create=False) / bundle
            if pdir.exists():
                shutil.rmtree(pdir)
                changed = True
                logging.info("[%s] plugin %s removed", install_root, bundle)
            else:
                logging.info("[%s] plugin %s already absent", install_root, bundle)
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
            bundle=bundle,
            package_url=package_url,
            token=config.mcc_token,
            state_filename=config.plugins_state_filename,
            state_payload={
                "bundle": bundle,
                "version": str(item.get("version", "")).strip(),
                "sha256": str(item.get("sha256", "")).strip(),
                "installed_at": datetime_now_iso(),
                "source": package_url,
            },
        )
        changed = True
        logging.info("[%s] plugin %s applied action=%s", install_root, bundle, action)

    if changed:
        _run_manifest_sql_fixes(install, selected)
        _apply_hostnet_mautic4_tx_patch(install, selected)
        _run_post_steps(config, install)
        print("Plugins applied and post-steps completed")
    else:
        print("No plugin changes required")
    return 0


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
