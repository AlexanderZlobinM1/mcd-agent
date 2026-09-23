from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4


SCHEMA = "mcd-web-availability-v1"
MIN_PROBE_INTERVAL_SEC = 30
FAILURE_THRESHOLD = 3
MAX_ATTEMPTS = 2
DEFAULT_COOLDOWN_SEC = 300
_ALLOWED_SERVICES = {"nginx", "php-fpm", "mariadb", "mcd"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_path(cfg: Any) -> Path:
    state_db = str(getattr(cfg, "state_db_path", "/opt/mcd/var/state.db") or "/opt/mcd/var/state.db")
    return Path(state_db).parent / "web-availability.json"


def _load_state(cfg: Any) -> dict[str, Any]:
    path = _state_path(cfg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(cfg: Any, state: dict[str, Any]) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _endpoint(
    cfg: Any,
    installs: list[object],
    *,
    previous_endpoint: str = "",
) -> tuple[str, str, str]:
    configured = str(getattr(cfg, "web_availability_endpoint", "") or "").strip()
    if configured:
        return configured, "host", ""
    candidates: list[tuple[str, str, str]] = []
    for inst in installs:
        uid = str(getattr(inst, "instance_uid", "") or "").strip()
        domain = str(getattr(inst, "primary_domain", "") or "").strip()
        if not domain:
            domains = getattr(inst, "domains", []) or []
            domain = str(domains[0] if domains else "").strip()
        if domain:
            endpoint = f"https://{domain}/"
            candidate = (endpoint, "instance", uid)
            if candidate not in candidates:
                candidates.append(candidate)
    if candidates:
        previous_index = next(
            (index for index, candidate in enumerate(candidates) if candidate[0] == previous_endpoint),
            -1,
        )
        return candidates[(previous_index + 1) % len(candidates)]
    return "", "host", ""


def _probe(endpoint: str, *, timeout_sec: int = 15) -> dict[str, Any]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return {"status": "unavailable", "phase": "unknown", "diagnosis": "unknown"}
    host = parsed.hostname
    port = int(parsed.port or 443)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    started = time.monotonic()
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return _failed("dns", "dns_failure", started)
    except OSError:
        return _failed("dns", "unknown", started)

    context = ssl.create_default_context()
    connection = None
    try:
        connection = __import__("http.client", fromlist=["HTTPSConnection"]).HTTPSConnection(
            host, port=port, timeout=max(3, int(timeout_sec)), context=context
        )
        connection.connect()
        connection.request("GET", path, headers={"Host": host, "Connection": "close"})
        response = connection.getresponse()
        status = int(response.status)
        response.read(1024)
    except ssl.SSLError:
        return _failed("tls", "tls_failure", started)
    except socket.timeout:
        return _failed("http", "timeout", started)
    except OSError:
        return _failed("http", "http_failure", started)
    finally:
        if connection is not None:
            connection.close()

    latency = max(0, int((time.monotonic() - started) * 1000))
    if 200 <= status < 400:
        return {"status": "available", "phase": "mautic", "diagnosis": "ok", "http_status": status, "latency_ms": latency}
    if status >= 500:
        return {"status": "unavailable", "phase": "php_fpm", "diagnosis": "upstream_failure", "http_status": status, "latency_ms": latency}
    return {"status": "unavailable", "phase": "http", "diagnosis": "http_failure", "http_status": status, "latency_ms": latency}


def _failed(phase: str, diagnosis: str, started: float) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "phase": phase,
        "diagnosis": diagnosis,
        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
    }


def _restart_service(service: str, *, timeout_sec: int = 60, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> tuple[bool, str]:
    target = service
    if service == "php-fpm":
        probe = runner(
            ["systemctl", "list-units", "--type=service", "--state=active", "php*-fpm.service", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        target = next((line.split()[0].removesuffix(".service") for line in (probe.stdout or "").splitlines() if line.split()), "")
        if not target:
            return False, "php-fpm service not found"
    if target not in _ALLOWED_SERVICES and not target.startswith("php"):
        return False, "service is not allowed"
    proc = runner(["systemctl", "restart", target], capture_output=True, text=True, timeout=timeout_sec, check=False)
    if proc.returncode == 0:
        return True, "success"
    return False, (proc.stderr or proc.stdout or "restart failed").strip()[:300]


def collect_web_availability(
    cfg: Any,
    installs: list[object],
    *,
    now: float | None = None,
    probe: Callable[..., dict[str, Any]] = _probe,
    restart: Callable[..., tuple[bool, str]] = _restart_service,
) -> dict[str, Any] | None:
    state = _load_state(cfg)
    endpoint, scope, instance_uid = _endpoint(
        cfg,
        installs,
        previous_endpoint=str(state.get("_rotation_last_endpoint") or ""),
    )
    if not endpoint:
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    now_ts = float(time.time() if now is None else now)
    key = f"{scope}:{instance_uid}:{endpoint}"
    previous = state.get(key) if isinstance(state.get(key), dict) else {}
    last_probe = float(previous.get("last_probe_ts", 0) or 0)
    if last_probe and now_ts - last_probe < MIN_PROBE_INTERVAL_SEC:
        cached = previous.get("observation")
        return dict(cached) if isinstance(cached, dict) else None

    observation = dict(probe(endpoint))
    available = observation.get("status") == "available"
    failures = 0 if available else int(previous.get("consecutive_failures", 0) or 0) + 1
    attempts = 0 if available else int(previous.get("attempts", 0) or 0)
    cooldown_until = float(previous.get("cooldown_until_ts", 0) or 0)
    remediation: dict[str, Any] = {
        "decision": "none",
        "service": None,
        "reason": "healthy" if available else "failure_threshold_not_reached",
        "attempt": attempts,
        "max_attempts": MAX_ATTEMPTS,
        "cooldown_until_utc": None,
        "result": "not_run",
    }
    if not available and failures >= FAILURE_THRESHOLD:
        diagnosis = str(observation.get("diagnosis") or "unknown")
        service = {"http_failure": "nginx", "timeout": "nginx", "upstream_failure": "php-fpm", "application_failure": "php-fpm"}.get(diagnosis)
        remediation["service"] = service
        if attempts >= MAX_ATTEMPTS:
            remediation.update({"decision": "manual_attention", "reason": "restart_attempts_exhausted", "result": "blocked", "attempt": attempts})
        elif cooldown_until > now_ts:
            remediation.update({"reason": "restart_cooldown_active", "result": "blocked", "cooldown_until_utc": datetime.fromtimestamp(cooldown_until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        elif not service:
            remediation.update({"decision": "manual_attention", "reason": "diagnosis_has_no_allowed_service", "result": "blocked"})
        else:
            remediation.update({"decision": "requested", "reason": "confirmed_https_failure", "attempt": attempts + 1})
            ok, detail = restart(service)
            attempts += 1
            cooldown_until = now_ts + max(60, int(getattr(cfg, "web_availability_cooldown_sec", DEFAULT_COOLDOWN_SEC) or DEFAULT_COOLDOWN_SEC))
            remediation.update({"decision": "completed" if ok else "failed", "result": "success" if ok else "failure", "attempt": attempts, "cooldown_until_utc": datetime.fromtimestamp(cooldown_until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            if not ok:
                remediation["reason"] = f"restart_failed:{detail}"

    observed_at = _utc_now()
    observation.update({
        "schema": SCHEMA,
        "scope": scope,
        "instance_uid": instance_uid or None,
        "endpoint": endpoint,
        "observed_at_utc": observed_at,
        "probe_id": uuid4().hex,
        "remediation": remediation,
    })
    state[key] = {
        "last_probe_ts": now_ts,
        "consecutive_failures": failures,
        "attempts": attempts,
        "cooldown_until_ts": cooldown_until,
        "observation": observation,
    }
    if scope == "instance":
        state["_rotation_last_endpoint"] = endpoint
    _save_state(cfg, state)
    return observation
