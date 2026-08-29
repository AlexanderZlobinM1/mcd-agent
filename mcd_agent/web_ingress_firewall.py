from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


WEB_FIREWALL_SERVICE_NAME = "mcd-web-firewall"
LOOPBACK_SERVICE_NAME = "mcd-web-firewall-loopback"
WEB_FIREWALL_SERVICE = Path(f"/etc/systemd/system/{WEB_FIREWALL_SERVICE_NAME}.service")
LOOPBACK_HELPER = Path(f"/usr/local/libexec/{LOOPBACK_SERVICE_NAME}")
LOOPBACK_SERVICE = Path(f"/etc/systemd/system/{LOOPBACK_SERVICE_NAME}.service")
WEB_FIREWALL_DROPIN = Path(f"/etc/systemd/system/{WEB_FIREWALL_SERVICE_NAME}.service.d/20-loopback.conf")


def _run(args: list[str], *, timeout_sec: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_sec, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()


def _write_atomic(path: Path, content: str, *, mode: int) -> bool:
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def _helper_content() -> str:
    return """#!/bin/sh
set -eu

ensure_loopback_before_web_filter() {
  IPTABLES="$1"
  CHAIN="$2"
  if ! "$IPTABLES" -w -S INPUT | grep -F -q -- "-j $CHAIN"; then
    return 0
  fi
  while "$IPTABLES" -w -C INPUT -i lo -j ACCEPT 2>/dev/null; do
    "$IPTABLES" -w -D INPUT -i lo -j ACCEPT
  done
  "$IPTABLES" -w -I INPUT 1 -i lo -j ACCEPT
}

ensure_loopback_before_web_filter iptables MCD_CF_WEB
if command -v ip6tables >/dev/null 2>&1; then
  ensure_loopback_before_web_filter ip6tables MCD_CF_WEB6
fi
"""


def _service_content() -> str:
    return f"""[Unit]
Description=MCD loopback precedence for managed Cloudflare web firewall
Requires={WEB_FIREWALL_SERVICE_NAME}.service
After={WEB_FIREWALL_SERVICE_NAME}.service
PartOf={WEB_FIREWALL_SERVICE_NAME}.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={LOOPBACK_HELPER}
"""


def _dropin_content() -> str:
    return f"""[Unit]
Wants={LOOPBACK_SERVICE_NAME}.service
Before={LOOPBACK_SERVICE_NAME}.service
"""


def ensure_managed_web_firewall_loopback() -> dict[str, object]:
    """Keep loopback ahead of the legacy MCD Cloudflare web ingress chain.

    Only the historical ``mcd-web-firewall`` service is adopted. Hosts without
    that MCD-owned service are left untouched, including independently managed
    firewall configurations.
    """

    if not WEB_FIREWALL_SERVICE.exists():
        return {"status": "skipped", "reason": "managed_web_firewall_absent", "changed": False}
    if os.geteuid() != 0:
        return {"status": "skipped", "reason": "root_required", "changed": False}

    changed_files: list[str] = []
    if _write_atomic(LOOPBACK_HELPER, _helper_content(), mode=0o755):
        changed_files.append(str(LOOPBACK_HELPER))
    if _write_atomic(LOOPBACK_SERVICE, _service_content(), mode=0o644):
        changed_files.append(str(LOOPBACK_SERVICE))
    if _write_atomic(WEB_FIREWALL_DROPIN, _dropin_content(), mode=0o644):
        changed_files.append(str(WEB_FIREWALL_DROPIN))

    rc, output = _run(["systemctl", "daemon-reload"])
    if rc != 0:
        return {
            "status": "error",
            "reason": "systemd_daemon_reload_failed",
            "detail": output,
            "changed": bool(changed_files),
            "changed_files": changed_files,
        }
    rc, output = _run(["systemctl", "restart", LOOPBACK_SERVICE_NAME])
    if rc != 0:
        return {
            "status": "error",
            "reason": "loopback_firewall_apply_failed",
            "detail": output,
            "changed": bool(changed_files),
            "changed_files": changed_files,
        }
    return {"status": "ok", "changed": bool(changed_files), "changed_files": changed_files}
