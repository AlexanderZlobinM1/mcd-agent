from __future__ import annotations

import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

from mcd_agent.config import AgentConfig
from mcd_agent.install_type import detect_install_type


AMAZON_MAILER_REQUIRED_BUNDLES: set[str] = {
    "AmazonSnsCallbackBundle",
    "MauticAmazonSesBundle",
}

AMAZON_MAILER_PACKAGE = "symfony/amazon-mailer"


def _run(
    cmd: list[str],
    *,
    cwd: str,
    timeout_sec: int = 900,
    as_www_data: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    full = cmd
    if as_www_data:
        full = ["sudo", "-u", "www-data"] + cmd
    logging.info("amazon-mailer preflight run: %s", " ".join(full))
    proc = subprocess.run(full, cwd=cwd, text=True, capture_output=True, timeout=timeout_sec)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(full)}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc


def _resolve_project_root(root: str) -> str:
    p = Path(root)
    candidates = [p, p.parent]
    for c in candidates:
        if (c / "composer.json").exists() and (c / "bin" / "console").exists():
            return str(c)
    return root


def _resolve_composer_bin() -> str:
    preferred = Path("/usr/local/bin/composer")
    if preferred.exists():
        return str(preferred)
    found = shutil.which("composer")
    if found:
        return found
    with tempfile.NamedTemporaryFile(prefix="composer-setup-", suffix=".php", delete=False) as tf:
        setup_path = Path(tf.name)
    try:
        with urllib.request.urlopen("https://getcomposer.org/installer", timeout=90) as resp:
            setup_path.write_bytes(resp.read())
        subprocess.run(
            ["php", str(setup_path), "--install-dir=/usr/local/bin", "--filename=composer"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        setup_path.unlink(missing_ok=True)
    return str(preferred)


def _node_major() -> int:
    found = shutil.which("node")
    if not found:
        return 0
    proc = subprocess.run([found, "-v"], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return 0
    raw = (proc.stdout or "").strip()
    m = re.search(r"v?(\d+)", raw)
    if not m:
        return 0
    return int(m.group(1))


def _ensure_node20() -> None:
    if _node_major() >= 20:
        return
    subprocess.run(["apt-get", "remove", "--purge", "-y", "nodejs", "libnode-dev"], check=False)
    subprocess.run(["bash", "-lc", "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"], check=True, timeout=300)
    subprocess.run(["apt-get", "install", "-y", "nodejs"], check=True, timeout=300)
    if _node_major() < 20:
        raise RuntimeError("Node.js 20 preflight failed (node -v is not v20+)")


def _composer_has_package(project_root: str, composer_bin: str, package_name: str) -> bool:
    proc = _run(
        [composer_bin, "show", package_name, "--no-interaction", "--no-ansi"],
        cwd=project_root,
        as_www_data=True,
        check=False,
        timeout_sec=180,
    )
    return proc.returncode == 0


def _normalize_console_path(project_root: str, console_path: str) -> str:
    if Path(console_path).is_absolute():
        return console_path
    return str(Path(project_root) / console_path)


def installed_required_bundles(root: str) -> set[str]:
    out: set[str] = set()
    candidates = [
        Path(root) / "plugins",
        Path(root) / "docroot" / "plugins",
        Path(root) / "public" / "plugins",
    ]
    for base in candidates:
        if not base.exists() or not base.is_dir():
            continue
        for name in AMAZON_MAILER_REQUIRED_BUNDLES:
            if (base / name).exists():
                out.add(name)
    return out


def ensure_amazon_mailer_for_bundles(
    *,
    config: AgentConfig,
    root: str,
    console_path: str,
    bundles: set[str],
    reason: str,
) -> bool:
    targets = set(bundles).intersection(AMAZON_MAILER_REQUIRED_BUNDLES)
    if not targets:
        return False

    install_type = detect_install_type(root)
    project_root = _resolve_project_root(root)
    composer_bin = _resolve_composer_bin()

    if _composer_has_package(project_root, composer_bin, AMAZON_MAILER_PACKAGE):
        logging.info("[%s] amazon-mailer already installed (%s)", root, reason)
        return False

    # For zip installs we also normalize Node.js runtime to v20 before composer require.
    if install_type == "zip":
        _ensure_node20()

    _run(
        [composer_bin, "require", AMAZON_MAILER_PACKAGE, "--no-interaction"],
        cwd=project_root,
        as_www_data=True,
        timeout_sec=max(int(config.command_timeout_sec or 900), 900),
    )

    console_abs = _normalize_console_path(project_root, console_path)
    _run(
        [config.php_bin, console_abs, "cache:clear"],
        cwd=project_root,
        as_www_data=True,
        timeout_sec=max(int(config.command_timeout_sec or 900), 600),
    )
    logging.info(
        "[%s] amazon-mailer dependency installed for bundles=%s (%s)",
        root,
        ",".join(sorted(targets)),
        reason,
    )
    return True

