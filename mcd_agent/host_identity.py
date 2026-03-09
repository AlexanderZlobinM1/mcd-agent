from __future__ import annotations

import socket

from mcd_agent.config import AgentConfig


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
    clone_detected = bool(
        is_template
        and autopromote_on_clone
        and configured_host_name
        and local_hostname
        and configured_host_name != local_hostname
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
        "source_host_name": configured_host_name or None,
    }
