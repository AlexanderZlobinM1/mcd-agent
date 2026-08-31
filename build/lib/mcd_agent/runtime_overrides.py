from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.config import AgentConfig, apply_runtime_overrides, runtime_remote_allowed_keys, runtime_overrides_from_config_file
from mcd_agent.host_identity import resolve_agent_identity


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 12) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=timeout_sec) as resp:
        body = (resp.read() or b"").decode("utf-8", errors="replace")
    raw = json.loads(body or "{}")
    return raw if isinstance(raw, dict) else {}


def _api_base(cfg: AgentConfig) -> str | None:
    if not cfg.mcc_url:
        return None
    return cfg.mcc_url.rstrip("/")


_INSTANCE_RUNTIME_KEYS = {
    "segment_whitelist_instance_settings",
    "campaign_whitelist_instance_settings",
    "page_hits_orphan_cleanup_instance_settings",
    "plugin_operation_instance_settings",
    "message_queue_instance_settings",
    "monitored_email_parser_instance_settings",
    "form_embed_instance_settings",
    "empty_leads_cleanup_instance_settings",
}


def _instance_keys(inst: object) -> list[str]:
    values = [
        getattr(inst, "instance_uid", None),
        getattr(inst, "root", None),
        getattr(inst, "name", None),
        getattr(inst, "primary_domain", None),
    ]
    domains = getattr(inst, "domains", None)
    if isinstance(domains, list):
        values.extend(domains)
    return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def instance_desired_states(runtime: dict[str, Any], installs: list[object]) -> dict[str, dict[str, Any]]:
    """Extract instance-scoped settings using immutable UIDs as canonical keys."""
    out: dict[str, dict[str, Any]] = {}
    for inst in installs:
        keys = _instance_keys(inst)
        if not keys:
            continue
        uid = keys[0]
        scoped: dict[str, Any] = {}
        for runtime_key in _INSTANCE_RUNTIME_KEYS:
            entries = runtime.get(runtime_key)
            if not isinstance(entries, dict):
                continue
            for key in keys:
                if key in entries:
                    scoped[runtime_key] = entries[key]
                    break
        if scoped:
            out[uid] = scoped
    return out


def merge_instance_desired_states(runtime: dict[str, Any], states: object) -> dict[str, Any]:
    """Overlay MCC instance state without relying on the local root path."""
    merged = dict(runtime)
    if not isinstance(states, dict):
        return merged
    for raw_uid, raw_state in states.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_state, dict):
            continue
        overrides = raw_state.get("runtime_overrides", raw_state)
        if not isinstance(overrides, dict):
            continue
        for key, value in overrides.items():
            if key not in _INSTANCE_RUNTIME_KEYS:
                continue
            current = merged.get(key)
            next_map = dict(current) if isinstance(current, dict) else {}
            next_map[uid] = value
            merged[key] = next_map
    return merged


def fetch_runtime_overrides(cfg: AgentConfig, *, instance_uids: list[str] | None = None) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "desired_state_protocol": 1,
        "instance_uids": list(dict.fromkeys(str(uid or "").strip() for uid in (instance_uids or []) if str(uid or "").strip())),
    }
    url = base + "/api/v1/agent/runtime-overrides"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        if not isinstance(out.get("runtime_overrides"), dict):
            out["runtime_overrides"] = {}
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}", "runtime_overrides": {}}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}", "runtime_overrides": {}}
    except Exception as e:
        return {"status": "error", "reason": str(e), "runtime_overrides": {}}


def push_runtime_overrides(
    cfg: AgentConfig,
    runtime_overrides: dict[str, Any],
    *,
    merge: bool = False,
    target: str = "observed",
    desired_state_revision: int | None = None,
    instance_desired_states: dict[str, dict[str, Any]] | None = None,
    instance_desired_state_revisions: dict[str, int] | None = None,
) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    target_mode = str(target or "observed").strip().lower()
    if target_mode not in {"observed", "desired"}:
        target_mode = "observed"
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "runtime_overrides": runtime_overrides if isinstance(runtime_overrides, dict) else {},
        "push_mode": "merge" if merge else "replace",
        "target": target_mode,
    }
    if target_mode == "desired":
        payload["desired_state_protocol"] = 1
        if isinstance(desired_state_revision, int):
            payload["desired_state_revision"] = desired_state_revision
        if isinstance(instance_desired_states, dict):
            payload["instance_desired_states"] = instance_desired_states
        if isinstance(instance_desired_state_revisions, dict):
            payload["instance_desired_state_revisions"] = instance_desired_state_revisions
    url = base + "/api/v1/agent/runtime-overrides"
    try:
        out = _post_json(url, payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def local_runtime_overrides(cfg: AgentConfig) -> dict[str, Any]:
    return runtime_overrides_from_config_file(cfg.config_file_path)


def overrides_fingerprint(overrides: dict[str, Any]) -> str:
    normalized = json.dumps(overrides, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def poll_trigger_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "runtime-overrides.poll"


def touch_poll_trigger(cfg: AgentConfig) -> str:
    p = poll_trigger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(time.time())), encoding="utf-8")
    return str(p)


def consume_poll_trigger(cfg: AgentConfig) -> bool:
    p = poll_trigger_path(cfg)
    if not p.exists():
        return False
    try:
        p.unlink()
    except Exception:
        return True
    return True


def apply_remote_overrides(base_cfg: AgentConfig, overrides: dict[str, Any]) -> dict[str, Any]:
    cfg2, applied, unsupported, blocked = apply_runtime_overrides(
        base_cfg,
        overrides,
        allowed_keys=runtime_remote_allowed_keys(),
    )
    return {
        "config": cfg2,
        "applied_keys": applied,
        "unsupported_keys": unsupported,
        "blocked_keys": blocked,
    }


def acknowledge_runtime_state(
    cfg: AgentConfig,
    *,
    scope: str,
    scope_key: str,
    revision: int,
    status: str,
    error: str = "",
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _api_base(cfg)
    if not base:
        return {"status": "disabled", "reason": "mcc_url_not_set"}
    ident = resolve_agent_identity(cfg)
    payload = {
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "agent_version": __version__,
        "desired_state_protocol": 1,
        "desired_state_ack": {
            "scope": str(scope),
            "scope_key": str(scope_key),
            "revision": int(revision),
            "status": str(status),
            "error": str(error or ""),
            "observed": observed if isinstance(observed, dict) else {},
        },
    }
    try:
        out = _post_json(base + "/api/v1/agent/runtime-overrides", payload, cfg.mcc_token, timeout_sec=12)
        out.setdefault("status", "error")
        return out
    except HTTPError as e:
        return {"status": "error", "reason": f"http_{e.code}"}
    except URLError as e:
        return {"status": "error", "reason": f"urlerror:{e.reason}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
