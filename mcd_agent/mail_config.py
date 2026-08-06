from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib import parse

from mcd_agent.amazon_mailer_dep import (
    AMAZON_MAILER_PACKAGE,
    SENDGRID_MAILER_PACKAGE,
    ensure_mailer_packages,
)
from mcd_agent.config import AgentConfig
from mcd_agent.local_mail import (
    _host_name,
    _local_php,
    _mcc_json,
    _run,
    _send_test_message,
    _set_php_parameter,
    _set_php_parameter_if_present,
    _write_atomic,
    configure_local_mail,
    disable_local_mail,
)


def fetch_mail_profile(cfg: AgentConfig, profile_id: str) -> dict[str, Any]:
    return _mcc_json(
        cfg,
        "/api/v1/agent/mail-config/material",
        query={"profile_id": str(profile_id or "").strip(), "host_name": _host_name(cfg)},
    )


def _profile_status(
    cfg: AgentConfig,
    profile_id: str,
    *,
    status: str,
    error: str = "",
    tested: bool = False,
) -> None:
    _mcc_json(
        cfg,
        "/api/v1/agent/mail-config/status",
        payload={
            "profile_id": str(profile_id),
            "mcc_host_name": _host_name(cfg),
            "status": status,
            "error": error,
            "tested": bool(tested),
        },
        timeout_sec=10,
    )


def _external_dsn(kind: str, settings: dict[str, Any], credentials: dict[str, Any]) -> tuple[str, set[str]]:
    if kind == "smtp":
        user = parse.quote(str(credentials.get("username") or ""), safe="")
        password = parse.quote(str(credentials.get("password") or ""), safe="")
        host = str(settings.get("host") or "").strip()
        port = int(settings.get("port") or 587)
        scheme = "smtps" if str(settings.get("encryption") or "starttls") == "tls" else "smtp"
        return f"{scheme}://{user}:{password}@{host}:{port}", set()
    if kind == "amazon_ses_api":
        access = parse.quote(str(credentials.get("access_key") or ""), safe="")
        secret = parse.quote(str(credentials.get("secret_key") or ""), safe="")
        region = parse.quote(str(settings.get("region") or "eu-central-1"), safe="")
        return f"ses+api://{access}:{secret}@default?region={region}", {AMAZON_MAILER_PACKAGE}
    if kind == "sendgrid_api":
        key = parse.quote(str(credentials.get("api_key") or ""), safe="")
        return f"sendgrid+api://{key}@default", {SENDGRID_MAILER_PACKAGE}
    raise RuntimeError(f"unsupported external mail transport: {kind}")


def _configure_external_mautic(root: str, settings: dict[str, Any], dsn: str) -> None:
    path = _local_php(root)
    text = path.read_text(encoding="utf-8", errors="replace")
    text = _set_php_parameter(text, "mailer_dsn", dsn)
    text = _set_php_parameter(text, "mailer_from_email", str(settings.get("from_email") or ""))
    text = _set_php_parameter_if_present(text, "mailer_from_name", str(settings.get("from_name") or "Sales Snap"))
    text = _set_php_parameter(text, "mailer_return_path", str(settings.get("return_path") or ""))
    stat = path.stat()
    _write_atomic(path, text, mode=stat.st_mode & 0o777)
    os.chown(path, stat.st_uid, stat.st_gid)


def _clear_mautic_cache(cfg: AgentConfig, root: str) -> None:
    console = Path(root) / "bin" / "console"
    if not console.exists():
        raise RuntimeError("Mautic bin/console not found")
    rc, out = _run(
        ["sudo", "-u", "www-data", str(cfg.php_bin or "/usr/bin/php"), str(console), "cache:clear"],
        timeout_sec=max(600, int(cfg.command_timeout_sec or 300)),
    )
    if rc != 0:
        raise RuntimeError("Mautic cache clear failed after mail configuration: " + out)


def apply_mail_profile(
    cfg: AgentConfig,
    *,
    profile_id: str,
    domain: str,
    root: str,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("mail-config apply must run as root")
    material = fetch_mail_profile(cfg, profile_id)
    if str(material.get("instance_domain") or "").strip().lower() != str(domain or "").strip().lower():
        raise RuntimeError("mail profile belongs to another instance")
    if str(material.get("current_host_name") or "").strip() != _host_name(cfg):
        raise RuntimeError("mail profile belongs to another host")
    kind = str(material.get("transport_type") or "").strip().lower()
    settings = material.get("settings") if isinstance(material.get("settings"), dict) else {}
    credentials = material.get("credentials") if isinstance(material.get("credentials"), dict) else {}
    recipient = str(material.get("last_test_recipient") or "").strip()
    name = str(material.get("name") or "Mail profile").strip()
    try:
        try:
            _profile_status(cfg, profile_id, status="applying")
        except Exception:
            pass
        if kind == "own_host":
            result = configure_local_mail(
                cfg,
                domain=domain,
                root=root,
                test_recipient=recipient,
                profile_name=name,
            )
        else:
            path = _local_php(root)
            before = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
            dsn, packages = _external_dsn(kind, settings, credentials)
            try:
                if packages:
                    ensure_mailer_packages(
                        config=cfg,
                        root=root,
                        packages=packages,
                        reason=f"MCC mail profile {name}",
                    )
                _configure_external_mautic(root, settings, dsn)
                _clear_mautic_cache(cfg, root)
                test_result = _send_test_message(
                    cfg,
                    root=root,
                    recipient=recipient,
                    profile_name=name,
                )
                disable_result = disable_local_mail(cfg, domain=domain)
            except Exception:
                _write_atomic(path, before, mode=stat.st_mode & 0o777)
                os.chown(path, stat.st_uid, stat.st_gid)
                try:
                    _clear_mautic_cache(cfg, root)
                except Exception:
                    pass
                raise
            result = {
                "status": "ok",
                "instance_domain": domain,
                "transport_type": kind,
                "packages": sorted(packages),
                "test_email": test_result,
                "own_host_disable": disable_result,
            }
        try:
            _profile_status(cfg, profile_id, status="tested", tested=True)
        except Exception:
            pass
        return {"profile_id": profile_id, "transport_type": kind, **result}
    except Exception as exc:
        try:
            _profile_status(cfg, profile_id, status="error", error=str(exc))
        except Exception:
            pass
        raise
