from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


FILTER_PATH = Path("/etc/fail2ban/filter.d/mcc-global.conf")
JAIL_PATH = Path("/etc/fail2ban/jail.d/98-mcd-security.local")
LOG_PATH = Path("/var/log/fail2ban-mcc-global.log")
JAIL_NAME = "mcc-global"
MANAGED_MARKER = "# Managed by MCD: Wazuh/Cloudflare local Fail2ban enforcement"


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 15) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def fetch_security_blocklist(cfg: AgentConfig) -> dict[str, Any]:
    if not cfg.mcc_url:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
    }
    try:
        return _post_json(
            cfg.mcc_url.rstrip("/") + "/api/v1/agent/security-blocklist",
            payload,
            cfg.mcc_token,
            timeout_sec=15,
        )
    except HTTPError as exc:
        return {"status": "error", "reason": f"http_{exc.code}"}
    except URLError as exc:
        return {"status": "error", "reason": f"urlerror:{exc.reason}"}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


def _normalize_ip_or_network(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty address")
    if "/" in raw:
        item = ipaddress.ip_network(raw, strict=False)
    else:
        item = ipaddress.ip_address(raw)
    if not item.is_global:
        raise ValueError("address is not global")
    return str(item)


def _normalize_addresses(values: Any) -> set[str]:
    rows = values if isinstance(values, list) else []
    result: set[str] = set()
    for raw in rows:
        try:
            result.add(_normalize_ip_or_network(raw))
        except Exception:
            continue
    return result


def _allowlisted(value: str, allowlist: set[str]) -> bool:
    try:
        target = ipaddress.ip_network(value, strict=False)
    except Exception:
        return True
    for raw in allowlist:
        try:
            allowed = ipaddress.ip_network(raw, strict=False)
        except Exception:
            continue
        if target.version != allowed.version:
            continue
        if target.subnet_of(allowed) or allowed.subnet_of(target):
            return True
    return False


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> bool:
    current = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else None
    if current == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return True


def _run(*args: str, timeout: int = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, env=env)


def _ensure_fail2ban_installed() -> tuple[bool, str]:
    client = shutil.which("fail2ban-client")
    if client:
        return False, client
    apt = shutil.which("apt-get")
    if not apt:
        raise RuntimeError("fail2ban is missing and apt-get is unavailable")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    proc = _run(apt, "install", "-y", "fail2ban", timeout=600, env=env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()[-1000:]
        raise RuntimeError(f"fail2ban install failed: {detail}")
    client = shutil.which("fail2ban-client")
    if not client:
        raise RuntimeError("fail2ban-client is missing after package installation")
    return True, client


def _jail_config(*, enabled: bool, allowlist: set[str], action: str) -> str:
    ignore = ["127.0.0.1/8", "::1", *sorted(allowlist)]
    return (
        f"{MANAGED_MARKER}\n"
        "[sshd]\n"
        "enabled = true\n"
        "mode = aggressive\n"
        "backend = systemd\n"
        f"ignoreip = {' '.join(ignore)}\n"
        "maxretry = 5\n"
        "findtime = 10m\n"
        "bantime = 1h\n"
        "bantime.increment = true\n"
        "bantime.factor = 2\n"
        "bantime.maxtime = 7d\n"
        f"action = {action}[name=sshd]\n"
        "\n"
        f"[{JAIL_NAME}]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        "filter = mcc-global\n"
        f"logpath = {LOG_PATH}\n"
        "backend = polling\n"
        f"ignoreip = {' '.join(ignore)}\n"
        "maxretry = 1\n"
        "findtime = 10m\n"
        "bantime = -1\n"
        f"action = {action}[name=mcc-global]\n"
    )


def _filter_config() -> str:
    return (
        f"{MANAGED_MARKER}\n"
        "[Definition]\n"
        "failregex = ^MCC-GLOBAL-BAN <HOST>$\n"
        "ignoreregex =\n"
    )


def _status_ips(client: str, jail: str) -> set[str]:
    proc = _run(client, "status", jail)
    if proc.returncode != 0:
        return set()
    for raw in proc.stdout.splitlines():
        if "Banned IP list:" in raw:
            return {item for item in raw.split("Banned IP list:", 1)[1].split() if item}
    return set()


def _client_ok(proc: subprocess.CompletedProcess[str], operation: str) -> None:
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()[-800:]
    raise RuntimeError(f"{operation} failed: {detail}")


def _chunks(values: list[str], size: int = 250) -> list[list[str]]:
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


def _address_batches(values: list[str], size: int = 250) -> list[list[str]]:
    by_family: dict[int, list[str]] = {4: [], 6: []}
    for value in values:
        try:
            family = ipaddress.ip_network(value, strict=False).version
        except Exception:
            continue
        by_family[family].append(value)
    return [batch for family in (4, 6) for batch in _chunks(by_family[family], size)]


def apply_security_blocklist_profile(profile: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(profile.get("enabled", False))
    if os.geteuid() != 0:
        return {"status": "error", "reason": "root_required", "changed": False}

    allowlist = _normalize_addresses(profile.get("allowlist"))
    desired = {
        item
        for item in _normalize_addresses(profile.get("blocked"))
        if not _allowlisted(item, allowlist)
    }
    client = shutil.which("fail2ban-client")
    if not enabled and not client and not JAIL_PATH.exists():
        return {"status": "noop", "reason": "disabled_not_installed", "changed": False, "poll_sec": profile.get("poll_sec", 60)}

    installed = False
    if enabled:
        installed, client = _ensure_fail2ban_installed()
    elif not client:
        return {"status": "noop", "reason": "disabled_client_missing", "changed": False, "poll_sec": profile.get("poll_sec", 60)}
    assert client is not None

    action = "nftables-allports" if shutil.which("nft") else "iptables-allports"
    before_filter = FILTER_PATH.read_text(encoding="utf-8", errors="ignore") if FILTER_PATH.is_file() else None
    before_jail = JAIL_PATH.read_text(encoding="utf-8", errors="ignore") if JAIL_PATH.is_file() else None
    changed_files = False
    changed_files |= _atomic_write(FILTER_PATH, _filter_config())
    changed_files |= _atomic_write(
        JAIL_PATH,
        _jail_config(enabled=enabled, allowlist=allowlist, action=action),
    )
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(exist_ok=True)
    os.chmod(LOG_PATH, 0o640)
    try:
        _client_ok(_run(client, "-t", timeout=120), "fail2ban config validation")
        _client_ok(_run("systemctl", "enable", "--now", "fail2ban", timeout=120), "fail2ban enable")
        if changed_files:
            _client_ok(_run(client, "reload", timeout=120), "fail2ban reload")
    except Exception:
        if before_filter is None:
            FILTER_PATH.unlink(missing_ok=True)
        else:
            _atomic_write(FILTER_PATH, before_filter)
        if before_jail is None:
            JAIL_PATH.unlink(missing_ok=True)
        else:
            _atomic_write(JAIL_PATH, before_jail)
        _run(client, "reload", timeout=120)
        raise

    if not enabled:
        current = _status_ips(client, JAIL_NAME)
        for ip_value in sorted(current):
            _run(client, "set", JAIL_NAME, "unbanip", ip_value)
        return {
            "status": "applied" if current or changed_files else "noop",
            "reason": "disabled",
            "changed": bool(current or changed_files),
            "removed": len(current),
            "poll_sec": profile.get("poll_sec", 60),
        }

    current = _status_ips(client, JAIL_NAME)
    added = sorted(desired - current)
    removed = sorted(current - desired)
    for batch in _address_batches(removed):
        _client_ok(
            _run(client, "set", JAIL_NAME, "unbanip", *batch, timeout=120),
            f"unban {len(batch)} addresses",
        )
    for batch in _address_batches(added):
        _client_ok(
            _run(client, "set", JAIL_NAME, "banip", *batch, timeout=120),
            f"ban {len(batch)} addresses",
        )
    for batch in _address_batches(sorted(allowlist)):
        _run(client, "set", "sshd", "unbanip", *batch, timeout=120)

    return {
        "status": "applied" if installed or changed_files or added or removed else "noop",
        "changed": bool(installed or changed_files or added or removed),
        "installed": installed,
        "desired": len(desired),
        "added": added,
        "removed": removed,
        "poll_sec": profile.get("poll_sec", 60),
        "generated_at": str(profile.get("generated_at") or ""),
    }


def sync_security_blocklist_once(cfg: AgentConfig) -> dict[str, Any]:
    fetched = fetch_security_blocklist(cfg)
    if str(fetched.get("status") or "").strip().lower() != "ok":
        return {"status": "error", "reason": fetched.get("reason", "fetch_failed"), "fetch": fetched}
    profile = fetched.get("profile")
    if not isinstance(profile, dict):
        return {"status": "error", "reason": "invalid profile payload", "fetch": fetched}
    applied = apply_security_blocklist_profile(profile)
    return {"status": str(applied.get("status") or "error"), "fetch": fetched, "apply": applied}
