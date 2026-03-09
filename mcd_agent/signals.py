from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
from pathlib import Path


def _run_journal(args: list[str], timeout_sec: int = 4) -> str:
    base = ["journalctl", "--no-pager", "-o", "cat"]
    cmd = base + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except Exception:
        return ""
    if p.returncode != 0:
        return p.stdout or ""
    return p.stdout or ""


def _systemd_list_units(patterns: list[str], *, active_only: bool) -> list[str]:
    cmd = ["systemctl"]
    if active_only:
        cmd += ["list-units", "--type=service", "--all"]
    else:
        cmd += ["list-unit-files", "--type=service"]
    cmd += ["--no-legend", "--no-pager"]
    cmd += patterns
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if p.returncode != 0:
        return []
    out: list[str] = []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if not unit.endswith(".service"):
            continue
        out.append(unit)
    return out


def _detect_php_fpm_units() -> list[str]:
    """
    Return PHP-FPM systemd units in preferred order:
    1) active units
    2) installed unit files
    """
    patterns = ["php*-fpm.service", "php-fpm.service"]
    active = _systemd_list_units(patterns, active_only=True)
    installed = _systemd_list_units(patterns, active_only=False)
    ordered: list[str] = []
    seen: set[str] = set()
    for unit in active + installed:
        if unit in seen:
            continue
        seen.add(unit)
        ordered.append(unit)
    return ordered


def _count(text: str, pattern: str) -> int:
    if not text:
        return 0
    return len(re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE))


def _tail_file(path: str, lines: int = 4000, timeout_sec: int = 3) -> str:
    try:
        p = subprocess.run(["tail", "-n", str(lines), path], capture_output=True, text=True, timeout=timeout_sec)
    except Exception:
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout or ""


def _parse_nginx_access_ts(line: str) -> datetime | None:
    # Example: [23/Feb/2026:11:00:02 +0000]
    m = re.search(r"\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+\-]\d{4})\]", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S %z")
    except Exception:
        return None


def _parse_nginx_error_ts(line: str) -> datetime | None:
    # Example: 2026/02/23 11:00:02
    m = re.match(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        local_tz = datetime.now().astimezone().tzinfo
        return datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S").replace(tzinfo=local_tz)
    except Exception:
        return None


def _count_nginx_file_signals(window_min: int) -> tuple[int, int]:
    now = datetime.now().astimezone()
    cutoff = now.timestamp() - (max(1, int(window_min)) * 60)

    access_paths = ["/var/log/nginx/access.log", "/var/log/nginx/access.log.1"]
    error_paths = ["/var/log/nginx/error.log", "/var/log/nginx/error.log.1"]

    http_5xx = 0
    web_critical = 0
    for ap in access_paths:
        if not Path(ap).exists():
            continue
        for line in _tail_file(ap, lines=4000).splitlines():
            ts = _parse_nginx_access_ts(line)
            if not ts or ts.timestamp() < cutoff:
                continue
            if re.search(r'"\s(50[0-9]|52[0-9])\s', line):
                http_5xx += 1

    web_err_pat = re.compile(
        r"while reading response header from upstream|upstream timed out|no live upstreams|connect\(\) failed|php message",
        flags=re.IGNORECASE,
    )
    for ep in error_paths:
        if not Path(ep).exists():
            continue
        for line in _tail_file(ep, lines=4000).splitlines():
            ts = _parse_nginx_error_ts(line)
            if not ts or ts.timestamp() < cutoff:
                continue
            if web_err_pat.search(line):
                web_critical += 1

    return http_5xx, web_critical


def collect_signals(window_min: int = 15) -> dict[str, object]:
    window = max(1, min(1440, int(window_min)))
    since = f"-{window} min"

    kernel = _run_journal(["-k", "--since", since, "-n", "2000"])
    mysql = "\n".join(
        [
            _run_journal(["-u", "mysql", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "mariadb", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "mysqld", "--since", since, "-n", "1200"]),
        ]
    )
    php_units = _detect_php_fpm_units()
    if not php_units:
        php_units = ["php-fpm.service"]
    php_fpm = "\n".join(
        _run_journal(["-u", unit, "--since", since, "-n", "1200"]) for unit in php_units
    )
    web = "\n".join(
        [
            _run_journal(["-u", "nginx", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "apache2", "--since", since, "-n", "1200"]),
            _run_journal(["-u", "httpd", "--since", since, "-n", "1200"]),
        ]
    )
    file_http_5xx, file_web_critical = _count_nginx_file_signals(window)

    totals = {
        "oom_kill": _count(kernel, r"oom-kill|out of memory|killed process \d+"),
        "mysql_critical": _count(
            mysql,
            r"table .* marked as crashed|lost connection to mysql server|innodb: .*?(fatal|assert|corrupt)|segmentation fault|signal \d+",
        ),
        "php_fpm_max_children": _count(php_fpm, r"server reached pm\.max_children"),
        "http_5xx": _count(web, r"\b(50[0-9]|52[0-9])\b") + file_http_5xx,
        "web_critical": file_web_critical,
    }

    comp = {
        "kernel": {"oom_kill": totals["oom_kill"]},
        "database": {"mysql_critical": totals["mysql_critical"]},
        "php_fpm": {"max_children": totals["php_fpm_max_children"]},
        "web": {"http_5xx": totals["http_5xx"], "critical_errors": totals["web_critical"]},
    }
    levels = {
        "kernel": min(5, totals["oom_kill"]),
        "database": min(5, totals["mysql_critical"]),
        "php_fpm": min(5, (totals["php_fpm_max_children"] // 2) + (1 if totals["php_fpm_max_children"] > 0 else 0)),
        "web": min(5, (totals["http_5xx"] // 10 + (1 if totals["http_5xx"] > 0 else 0)) + (1 if totals["web_critical"] > 0 else 0)),
    }
    overall = max(levels.values()) if levels else 0
    status = "ok" if overall == 0 else ("warn" if overall <= 2 else "critical")

    return {
        "window_min": window,
        "collected_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall": {"level": overall, "status": status},
        "components": {
            "kernel": {"level": levels["kernel"], "signals": comp["kernel"]},
            "database": {"level": levels["database"], "signals": comp["database"]},
            "php_fpm": {"level": levels["php_fpm"], "signals": comp["php_fpm"]},
            "web": {"level": levels["web"], "signals": comp["web"]},
        },
        "totals": totals,
    }


def format_signals_text(payload: dict[str, object]) -> str:
    overall = payload.get("overall", {}) if isinstance(payload, dict) else {}
    totals = payload.get("totals", {}) if isinstance(payload, dict) else {}
    lines = [
        f"status={overall.get('status', '-')}",
        f"level={overall.get('level', '-')}",
        f"window_min={payload.get('window_min', '-')}",
        f"collected_at_utc={payload.get('collected_at_utc', '-')}",
    ]
    if isinstance(totals, dict):
        for k in ("oom_kill", "mysql_critical", "php_fpm_max_children", "http_5xx", "web_critical"):
            lines.append(f"{k}={int(totals.get(k, 0) or 0)}")
    return "\n".join(lines)


def format_signals_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
