from __future__ import annotations

import json
import socket
from pathlib import Path

from mcd_agent.config import AgentConfig

_TEMPLATE_IDENTITY_PATH = Path("/opt/mcd/var/template_identity.json")


def _read_template_source_host_name() -> str:
    try:
        if not _TEMPLATE_IDENTITY_PATH.exists():
            return ""
        raw = json.loads(_TEMPLATE_IDENTITY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ""
        return str(raw.get("source_host_name", "")).strip()
    except Exception:
        return ""


def _write_template_source_host_name(source_host_name: str) -> None:
    src = str(source_host_name or "").strip()
    if not src:
        return
    try:
        _TEMPLATE_IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"source_host_name": src}
        _TEMPLATE_IDENTITY_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return


def resolve_agent_identity(cfg: AgentConfig) -> dict[str, object]:
    """
    Resolve host identity used for MCC API calls.

    For template clones:
    - original configured MCC host name is treated as source host
    - effective hostname switches to local OS hostname
    - mcc_host_name is intentionally blank to avoid matching source host row
    """
    local_hostname = (socket.gethostname() or "").strip() or "localhost"
    configured_host_name = (cfg.mcc_host_name or "").strip()
    is_template = bool(getattr(cfg, "host_template", False))
    autopromote_on_clone = bool(getattr(cfg, "template_autopromote_on_clone", True))
    marker_source = _read_template_source_host_name() if is_template else ""
    source_host_name = configured_host_name or marker_source
    if is_template and not source_host_name:
        source_host_name = local_hostname
        _write_template_source_host_name(source_host_name)

    clone_detected = bool(
        is_template
        and autopromote_on_clone
        and source_host_name
        and local_hostname
        and source_host_name != local_hostname
    )
    effective_hostname = local_hostname if clone_detected else (configured_host_name or local_hostname)
    effective_mcc_host_name = "" if clone_detected else configured_host_name
    return {
        "local_hostname": local_hostname,
        "configured_host_name": configured_host_name or None,
        "effective_hostname": effective_hostname,
        "effective_mcc_host_name": effective_mcc_host_name,
        "is_template": is_template,
        "autopromote_on_clone": autopromote_on_clone,
        "clone_detected": clone_detected,
        "source_host_name": source_host_name or None,
    }
