from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib import error as urlerror
import urllib.request

from mcd_agent.config import AgentConfig
from mcd_agent.discovery import discover_mautic
from mcd_agent.install_type import detect_install_type
from mcd_agent.amazon_mailer_dep import ensure_amazon_mailer_for_bundles, installed_required_bundles
from mcd_agent.localphp import parse_local_php


CORE_PLUGIN_BUNDLES = {
    "GrapesJsBuilderBundle",
    "MauticClearbitBundle",
    "MauticCloudStorageBundle",
    "MauticCrmBundle",
    "MauticEmailMarketingBundle",
    "MauticFocusBundle",
    "MauticFullContactBundle",
    "MauticGmailBundle",
    "MauticOutlookBundle",
    "MauticSocialBundle",
    "MauticTagManagerBundle",
    "MauticZapierBundle",
}

FALLBACK_BRANCH_TARGETS: dict[str, str] = {
    "4.4": "4.4.13",
    "5.1": "5.1.1",
    "5.2": "5.2.9",
    "6.0": "6.0.7",
    "7.0": "7.0.0",
}


def _pick_install(config: AgentConfig, root: str | None) -> tuple[str, str]:
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst.root, inst.console_path
        raise RuntimeError(f"Mautic install not found: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        raise RuntimeError("Multiple installs found, pass --root")
    inst = installs[0]
    return inst.root, inst.console_path


def _branch_key(version: str) -> str:
    sv = _parse_semver(version)
    if sv == (0, 0, 0):
        return ""
    return f"{sv[0]}.{sv[1]}"


def _default_zip_url(version: str) -> str:
    return f"https://github.com/mautic/mautic/releases/download/{version}/{version}-update.zip"


def _release_targets_from_mcc(config: AgentConfig) -> dict[str, dict[str, str]]:
    if not config.mcc_url:
        return {}
    url = config.mcc_url.rstrip("/") + "/api/v1/agent/mautic/releases"
    req = urllib.request.Request(url=url, method="GET")
    if config.mcc_token:
        req.add_header("Authorization", f"Bearer {config.mcc_token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = (resp.read() or b"").decode("utf-8", errors="replace")
        raw = json.loads(body or "{}")
    except (urlerror.URLError, urlerror.HTTPError, TimeoutError, ValueError):
        return {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    branches = raw.get("branches", {})
    if not isinstance(branches, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for branch, row in branches.items():
        if not isinstance(row, dict):
            continue
        bk = str(branch).strip()
        ver = str(row.get("version", "")).strip()
        if not bk or not ver:
            continue
        zip_url = str(row.get("zip_url", "")).strip() or _default_zip_url(ver)
        out[bk] = {"version": ver, "zip_url": zip_url}
    return out


def _release_targets_fallback() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for bk, ver in FALLBACK_BRANCH_TARGETS.items():
        out[bk] = {"version": ver, "zip_url": _default_zip_url(ver)}
    return out


def _release_targets(config: AgentConfig) -> dict[str, dict[str, str]]:
    out = _release_targets_fallback()
    remote = _release_targets_from_mcc(config)
    if remote:
        out.update(remote)
    return out


def _available_targets(config: AgentConfig) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _release_targets(config).values():
        ver = str(row.get("version", "")).strip()
        if not ver:
            continue
        out[ver] = str(row.get("zip_url", "")).strip() or _default_zip_url(ver)
    cache_dir = Path("/opt/mcd/cache/updates")
    if cache_dir.exists():
        for p in cache_dir.glob("*-update.zip"):
            m = re.search(r"(\d+\.\d+\.\d+)-update\.zip$", p.name)
            if not m:
                continue
            v = m.group(1)
            out[v] = str(p)
    return out


def _parse_semver(raw: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _read_current_version(root: str, console_path: str, php_bin: str) -> str:
    cmds = [
        [php_bin, console_path, "--version"],
        [php_bin, console_path, "about", "--no-interaction"],
    ]
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    lock = Path(root) / "composer.lock"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            for pkg in data.get("packages", []):
                if not isinstance(pkg, dict):
                    continue
                if str(pkg.get("name", "")) in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                    v = str(pkg.get("version", ""))
                    m = re.search(r"(\d+\.\d+\.\d+)", v)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return "0.0.0"


def _latest_same_branch(config: AgentConfig, version: str) -> str | None:
    targets = _available_targets(config)
    v = _parse_semver(version)
    if v == (0, 0, 0):
        return None
    candidates = [x for x in targets.keys() if _parse_semver(x)[:2] == v[:2]]
    if not candidates:
        return None
    latest = max(candidates, key=lambda x: _parse_semver(x))
    return latest if _parse_semver(latest) > v else None


def _resolve_update_package(config: AgentConfig, target: str) -> Path:
    targets = _available_targets(config)
    src = targets.get(target)
    if not src:
        raise RuntimeError(f"No package source for target {target}")
    p = Path(src)
    if p.exists():
        return p
    # src is URL from static targets
    cache_dir = Path("/opt/mcd/cache/updates")
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{target}-update.zip"
    if dst.exists():
        return dst
    with urllib.request.urlopen(src, timeout=120) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)
    return dst


def _run(cmd: list[str], *, cwd: str, as_www_data: bool = False) -> None:
    full = cmd
    if as_www_data:
        full = ["sudo", "-u", "www-data"] + cmd
    logging.info("run: %s", " ".join(full))
    proc = subprocess.run(full, cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(full)}\n{proc.stdout}\n{proc.stderr}")


def _backup_install(root: str) -> Path:
    backups = Path("/opt/mcd/backups")
    backups.mkdir(parents=True, exist_ok=True)
    out = backups / f"mautic-backup-{Path(root).name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="mcd-backup-") as td:
        tmp = Path(td) / "backup.tar.gz"
        subprocess.run(["tar", "-czf", str(tmp), "-C", str(Path(root).parent), str(Path(root).name)], check=True)
        shutil.move(str(tmp), out)
    return out


def _prepare_plugins_for_major_jump(root: str, from_ver: str, to_ver: str) -> None:
    if from_ver != "4.4.13" or to_ver != "5.1.1":
        return
    plugins_dir = Path(root) / "plugins"
    if not plugins_dir.exists():
        return
    bdir = Path("/opt/mcd/backups/plugins-pre-5.1.1")
    if bdir.exists():
        shutil.rmtree(bdir)
    shutil.copytree(plugins_dir, bdir)
    for d in plugins_dir.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name in CORE_PLUGIN_BUNDLES:
            continue
        shutil.rmtree(d)


def _apply_system_upgrade(from_ver: str, to_ver: str) -> None:
    if from_ver == "4.4.13" and to_ver == "5.1.1":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.2-apcu",
                "php8.2-fpm",
                "php8.2-cli",
                "php8.2-curl",
                "php8.2-mysql",
                "php8.2-mailparse",
                "php8.2-gd",
                "php8.2-mbstring",
                "php8.2-imagick",
                "php8.2-bcmath",
                "php8.2-zip",
                "php8.2-tidy",
                "php8.2-soap",
                "php8.2-intl",
                "php8.2-xml",
                "php8.2-imap",
                "php8.2-redis",
                "php8.2-opcache",
                "-y",
            ],
            check=True,
        )
        _run(["sed", "-i", "s/php8\\.0-fpm/php8.2-fpm/g", "/etc/nginx/sites-enabled/*"], cwd="/")
        _run(["service", "nginx", "restart"], cwd="/")
        subprocess.run(["apt", "purge", "php8.0*", "-y"], check=True)
    if to_ver == "6.0.7":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.3-apcu",
                "php8.3-fpm",
                "php8.3-cli",
                "php8.3-curl",
                "php8.3-mysql",
                "php8.3-mailparse",
                "php8.3-gd",
                "php8.3-mbstring",
                "php8.3-imagick",
                "php8.3-bcmath",
                "php8.3-zip",
                "php8.3-tidy",
                "php8.3-soap",
                "php8.3-intl",
                "php8.3-xml",
                "php8.3-imap",
                "php8.3-redis",
                "php8.3-opcache",
                "-y",
            ],
            check=True,
        )
    if to_ver == "7.0.0":
        subprocess.run(
            [
                "apt",
                "install",
                "php8.4-apcu",
                "php8.4-fpm",
                "php8.4-cli",
                "php8.4-curl",
                "php8.4-mysql",
                "php8.4-mailparse",
                "php8.4-gd",
                "php8.4-mbstring",
                "php8.4-imagick",
                "php8.4-bcmath",
                "php8.4-zip",
                "php8.4-tidy",
                "php8.4-soap",
                "php8.4-intl",
                "php8.4-xml",
                "php8.4-imap",
                "php8.4-redis",
                "php8.4-opcache",
                "-y",
            ],
            check=True,
        )


def _insert_migration_hacks_if_needed(root: str, to_ver: str) -> None:
    if to_ver != "6.0.7":
        return
    local_php = Path(root) / "config" / "local.php"
    if not local_php.exists():
        local_php = Path(root) / "app" / "config" / "local.php"
    if not local_php.exists():
        return
    cfg = parse_local_php(str(local_php))
    prefix = str(cfg.get("db_table_prefix", ""))
    table = f"{prefix}migrations"
    host = str(cfg.get("db_host", "localhost"))
    port = str(cfg.get("db_port", "3306"))
    name = str(cfg.get("db_name", ""))
    user = str(cfg.get("db_user", ""))
    pwd = str(cfg.get("db_password", ""))
    if not (name and user):
        return
    sql = (
        f"INSERT INTO `{table}` (version, executed_at, execution_time) VALUES "
        "(CONCAT('Mautic', CHAR(92), 'Migrations', CHAR(92), 'Version20211020114811'), NOW(), 0),"
        "(CONCAT('Mautic', CHAR(92), 'Migrations', CHAR(92), 'Version20230621074925'), NOW(), 0);"
    )
    cmd = [
        "mysql",
        f"-h{host}",
        f"-P{port}",
        f"-u{user}",
        f"-p{pwd}",
        name,
        "-e",
        sql,
    ]
    subprocess.run(cmd, check=True)


def _apply_zip(config: AgentConfig, root: str, console_path: str, php_bin: str, target: str) -> None:
    pkg = _resolve_update_package(config, target)
    dst = Path(root) / pkg.name
    shutil.copy2(pkg, dst)
    _run([php_bin, console_path, "mautic:update:apply", "--force", f"--update-package={dst.name}"], cwd=root, as_www_data=True)
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=root, as_www_data=True)


def _replace_version_tokens(text: str, current: str, target: str) -> tuple[str, int]:
    if not current or not target or current == target:
        return text, 0
    count = 0
    updated = text
    # Replace exact semantic version tokens only, keeps unrelated values intact.
    for src, dst in ((f"v{current}", f"v{target}"), (current, target)):
        rx = re.compile(rf"(?<![0-9A-Za-z]){re.escape(src)}(?![0-9A-Za-z])")
        updated, n = rx.subn(dst, updated)
        count += n
    return updated, count


def _resolve_composer_project_root(root: str) -> str:
    p = Path(root)
    candidates = [p, p.parent]
    for c in candidates:
        if (c / "composer.json").exists() and (c / "bin" / "console").exists():
            return str(c)
    return root


def _apply_composer(root: str, console_path: str, php_bin: str, current: str, target: str) -> None:
    project_root = _resolve_composer_project_root(root)
    cjson = Path(project_root) / "composer.json"
    if not cjson.exists():
        raise RuntimeError("composer.json not found")
    text = cjson.read_text(encoding="utf-8")
    updated, changes = _replace_version_tokens(text, current, target)
    if changes > 0 and updated != text:
        cjson.write_text(updated, encoding="utf-8")
    _run(["composer", "update", "--with-dependencies"], cwd=project_root, as_www_data=True)
    _run([php_bin, console_path, "cache:clear"], cwd=project_root, as_www_data=True)
    _run([php_bin, console_path, "mautic:update:apply", "--finish"], cwd=project_root, as_www_data=True)
    _run([php_bin, console_path, "doctrine:migration:migrate", "--no-interaction"], cwd=project_root, as_www_data=True)


def run_upgrade_check(config: AgentConfig, root: str | None) -> int:
    install_root, console = _pick_install(config, root)
    current = _read_current_version(install_root, console, config.php_bin)
    target = _latest_same_branch(config, current)
    branch = _branch_key(current)
    print(f"root={install_root}")
    print(f"current={current}")
    if branch:
        print(f"branch={branch}.x")
    if target is None:
        print("next=none")
    else:
        print(f"next={target}")
    return 0


def run_upgrade_apply(
    *,
    config: AgentConfig,
    root: str | None,
    mode: str,
    yes: bool,
    do_backup: bool,
    with_system_upgrade: bool,
    target_override: str | None = None,
) -> int:
    install_root, console = _pick_install(config, root)
    current = _read_current_version(install_root, console, config.php_bin)
    target = target_override or _latest_same_branch(config, current)
    if not target:
        print(f"No upgrade target for current version {current}")
        return 0
    if _parse_semver(target)[:2] != _parse_semver(current)[:2]:
        raise RuntimeError(
            f"Major/minor upgrade is disabled in current flow: {current} -> {target}. "
            "Only patch updates in current branch are allowed."
        )

    print(f"Upgrade plan: {current} -> {target} (mode={mode})")
    if not yes:
        ans = input("Proceed? [y/N]: ").strip().lower()
        if ans not in {"y", "yes"}:
            print("Cancelled")
            return 0

    if do_backup:
        b = _backup_install(install_root)
        print(f"Backup created: {b}")

    chosen_mode = mode
    if chosen_mode == "auto":
        chosen_mode = detect_install_type(install_root)

    if chosen_mode == "zip":
        _apply_zip(config, install_root, console, config.php_bin, target)
    elif chosen_mode == "composer":
        _apply_composer(install_root, console, config.php_bin, current, target)
    else:
        raise RuntimeError(f"Unsupported mode: {mode}")

    if with_system_upgrade:
        _apply_system_upgrade(current, target)

    ensure_amazon_mailer_for_bundles(
        config=config,
        root=install_root,
        console_path=console,
        bundles=installed_required_bundles(install_root),
        reason="mautic-upgrade",
    )

    print(f"Upgrade completed: {current} -> {target}")
    return 0


def run_upgrade_interactive(config: AgentConfig, root: str | None) -> int:
    install_root, console = _pick_install(config, root)
    current = _read_current_version(install_root, console, config.php_bin)
    target_next = _latest_same_branch(config, current)
    branch = _branch_key(current)
    print(f"root={install_root}")
    print(f"current={current}")
    if branch:
        print(f"branch={branch}.x")
    if not target_next:
        print("next=none")
        return 0
    print(f"latest_patch={target_next}")
    print("")
    print("Select target:")
    print("1. latest patch in current branch (recommended)")
    print("0. exit")
    t_choice = _ask("Target [1/0, default 1]: ").strip() or "1"
    if t_choice == "0":
        print("Cancelled")
        return 0
    target = target_next
    if not target:
        print("No valid target selected")
        return 0
    print("")
    print("Select upgrade mode:")
    print("1. auto (recommended)")
    print("2. zip")
    print("3. composer")
    print("0. exit")
    choice = _ask("Mode [1/2/3/0, default 1]: ").strip() or "1"
    if choice == "0":
        print("Cancelled")
        return 0
    mode_map = {"1": "auto", "2": "zip", "3": "composer"}
    mode = mode_map.get(choice, "auto")
    print("Backup note: MCD backup archives only the Mautic install directory.")
    backup = _ask("Create backup before upgrade? [Y/n]: ").strip().lower() not in {"n", "no"}
    sys_up = False
    print("")
    print(f"Plan: {current} -> {target} (mode={mode}, backup={backup}, with_system_upgrade=false)")
    confirm = _ask("Apply now? [y/N, 0=exit]: ").strip().lower()
    if confirm in {"0", "x", "exit", "q"}:
        print("Cancelled")
        return 0
    if confirm not in {"y", "yes"}:
        print("Cancelled")
        return 0
    return run_upgrade_apply(
        config=config,
        root=install_root,
        mode=mode,
        yes=True,
        do_backup=backup,
        with_system_upgrade=sys_up,
        target_override=target,
    )


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""
