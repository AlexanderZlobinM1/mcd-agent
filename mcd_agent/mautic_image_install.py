from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any
from urllib import request

from mcd_agent.config import AgentConfig
from mcd_agent.inventory import InstanceInventory


@dataclass
class ImageInstallPlan:
    image_ref: str
    domain: str
    short_name: str
    db_name: str
    db_user: str
    db_password: str
    webroot: Path
    php_version: str


def _run(args: list[str], *, timeout_sec: int = 300, input_text: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()


def _domain(raw: str) -> str:
    domain = str(raw or "").strip().lower().rstrip(".")
    if not re.match(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", domain):
        raise RuntimeError(f"invalid domain: {raw}")
    return domain


def _short(domain: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", domain.split(".", 1)[0]).strip("_")
    if not value:
        raise RuntimeError("empty short name")
    return value[:48].rstrip("_")


def _mysql_bin() -> str:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("mysql/mariadb client is missing")


def _mysql_exec(sql: str, *, timeout_sec: int = 120) -> str:
    rc, out = _run([_mysql_bin(), "-N", "-B", "-e", sql], timeout_sec=timeout_sec)
    if rc != 0:
        raise RuntimeError(out or "mysql command failed")
    return out


def _db_exists(name: str) -> bool:
    safe = name.replace("'", "''")
    out = _mysql_exec(f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='{safe}'")
    return bool(out.strip())


def _quote_ident(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _quote_sql(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_plan(*, image_ref: str, domain: str, php_version: str) -> ImageInstallPlan:
    d = _domain(domain)
    short = _short(d)
    db_name = f"baza_{short}"
    return ImageInstallPlan(
        image_ref=str(image_ref or "").strip(),
        domain=d,
        short_name=short,
        db_name=db_name,
        db_user=f"korisnik_{short}",
        db_password=secrets.token_urlsafe(24),
        webroot=Path("/var/www") / short / "public_html",
        php_version=str(php_version or "").strip(),
    )


def _preflight(plan: ImageInstallPlan) -> list[str]:
    problems: list[str] = []
    if os.geteuid() != 0:
        problems.append("must run as root")
    if not re.match(r"^[0-9]+\.[0-9]+$", plan.php_version):
        problems.append("invalid php version")
    for cmd in ("tar", "gzip", "certbot", "nginx", "php", "composer", "node", "npm"):
        if not shutil.which(cmd):
            problems.append(f"missing command: {cmd}")
    php_sock = Path(f"/run/php/php{plan.php_version}-fpm.sock")
    if not php_sock.exists():
        problems.append(f"missing PHP-FPM socket: {php_sock}")
    if plan.webroot.exists():
        problems.append(f"webroot already exists: {plan.webroot}")
    site_avail = Path("/etc/nginx/sites-available") / f"{plan.domain}.conf"
    site_enabled = Path("/etc/nginx/sites-enabled") / f"{plan.domain}.conf"
    if site_avail.exists():
        problems.append(f"nginx vhost already exists: {site_avail}")
    if site_enabled.exists() or site_enabled.is_symlink():
        problems.append(f"nginx enabled symlink already exists: {site_enabled}")
    try:
        if _db_exists(plan.db_name):
            problems.append(f"database already exists: {plan.db_name}")
    except Exception as exc:
        problems.append(f"database preflight failed: {exc}")
    return problems


def _download(url: str, token: str, dst: Path) -> None:
    req = request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with request.urlopen(req, timeout=1800) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _mysql_import_gz(path: Path, db_name: str) -> None:
    proc = subprocess.Popen(
        [_mysql_bin(), db_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    try:
        with gzip.open(path, "rb") as f:
            shutil.copyfileobj(f, proc.stdin)
        proc.stdin.close()
        proc.stdin = None
        out_b, err_b = proc.communicate(timeout=1800)
    except Exception:
        proc.kill()
        raise
    if proc.returncode != 0:
        out = ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace").strip()
        raise RuntimeError("database import failed: " + out)


def _artifact_url(cfg: AgentConfig, image_ref: str, kind: str) -> str:
    base = str(cfg.mcc_url or "").rstrip("/")
    if not base:
        raise RuntimeError("mcc.url is not configured")
    return f"{base}/api/v1/agent/mautic-images/{image_ref}/artifact/{kind}"


def _patch_local_php(path: Path, plan: ImageInstallPlan) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("/var/www/ss/public_html", str(plan.webroot))
    text = re.sub(r"default[0-9a-z-]*\.sales-snap\.(?:com|ru)", plan.domain, text, flags=re.IGNORECASE)

    def php_string(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def set_param(src: str, key: str, value: str) -> str:
        pattern = rf"(['\"]{re.escape(key)}['\"]\s*=>\s*)['\"][^'\"]*['\"]"
        quoted = "'" + php_string(value) + "'"
        new, count = re.subn(pattern, lambda m: m.group(1) + quoted, src)
        if count:
            return new
        marker = ");"
        insert = f"    '{key}' => {quoted},\n"
        idx = new.rfind(marker)
        if idx >= 0:
            return new[:idx] + insert + new[idx:]
        return new + "\n" + insert

    for key, value in {
        "db_host": "localhost",
        "db_port": "3306",
        "db_name": plan.db_name,
        "db_user": plan.db_user,
        "db_password": plan.db_password,
        "site_url": "https://" + plan.domain,
    }.items():
        text = set_param(text, key, value)
    path.write_text(text, encoding="utf-8")


def _write_nginx_vhost(plan: ImageInstallPlan) -> Path:
    site = Path("/etc/nginx/sites-available") / f"{plan.domain}.conf"
    site.parent.mkdir(parents=True, exist_ok=True)
    content = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {plan.domain};
    root {plan.webroot};
    index index.php index.html;
    client_max_body_size 128M;

    location / {{
        try_files $uri /index.php$is_args$args;
    }}

    location ~ \\.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php{plan.php_version}-fpm.sock;
    }}

    location ~* ^/(?:var|vendor|config/local\\.php|app/config/local\\.php) {{
        deny all;
    }}
}}
"""
    site.write_text(content, encoding="utf-8")
    enabled = Path("/etc/nginx/sites-enabled") / site.name
    enabled.parent.mkdir(parents=True, exist_ok=True)
    enabled.symlink_to(site)
    return site


def _safe_extract(tar: tarfile.TarFile, dst: Path) -> None:
    root = dst.resolve()
    for member in tar.getmembers():
        target = (dst / member.name).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"unsafe path in image archive: {member.name}")
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if root != link_target and root not in link_target.parents:
                raise RuntimeError(f"unsafe link in image archive: {member.name} -> {member.linkname}")
    tar.extractall(dst)


def install_from_image(
    cfg: AgentConfig,
    *,
    image_ref: str,
    domain: str,
    php_version: str,
    yes: bool = False,
    run_certbot: bool = True,
) -> dict[str, Any]:
    plan = build_plan(image_ref=image_ref, domain=domain, php_version=php_version)
    if not yes:
        raise RuntimeError("--yes is required")
    if not str(cfg.mcc_token or "").strip():
        raise RuntimeError("mcc.token is not configured")

    print(f"Preflight: domain={plan.domain} webroot={plan.webroot} db={plan.db_name}")
    problems = _preflight(plan)
    if problems:
        for p in problems:
            print(f"preflight_error: {p}")
        raise RuntimeError("preflight failed")

    with tempfile.TemporaryDirectory(prefix="mcd-image-install-") as td:
        tmp = Path(td)
        files_tgz = tmp / "files.tar.gz"
        db_gz = tmp / "db.sql.gz"
        print("Downloading image files")
        _download(_artifact_url(cfg, plan.image_ref, "files"), str(cfg.mcc_token), files_tgz)
        print("Downloading image database")
        _download(_artifact_url(cfg, plan.image_ref, "db"), str(cfg.mcc_token), db_gz)

        print("Creating database and user")
        _mysql_exec(f"CREATE DATABASE {_quote_ident(plan.db_name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        _mysql_exec(
            "CREATE USER IF NOT EXISTS "
            + _quote_sql(plan.db_user)
            + "@'localhost' IDENTIFIED BY "
            + _quote_sql(plan.db_password)
        )
        _mysql_exec(f"GRANT ALL PRIVILEGES ON {_quote_ident(plan.db_name)}.* TO {_quote_sql(plan.db_user)}@'localhost'")
        _mysql_exec("FLUSH PRIVILEGES")

        print("Extracting files")
        plan.webroot.parent.mkdir(parents=True, exist_ok=False)
        plan.webroot.mkdir(parents=True, exist_ok=False)
        with tarfile.open(files_tgz, "r:gz") as tf:
            _safe_extract(tf, plan.webroot)

        local_php = plan.webroot / "config" / "local.php"
        if not local_php.exists():
            local_php = plan.webroot / "app" / "config" / "local.php"
        if not local_php.exists():
            raise RuntimeError("local.php not found after extraction")
        print("Patching Mautic local.php")
        _patch_local_php(local_php, plan)

        print("Importing database")
        _mysql_import_gz(db_gz, plan.db_name)

        print("Fixing permissions")
        _run(["chown", "-R", "www-data:www-data", str(plan.webroot.parent)], timeout_sec=300)
        for rel in ("var/cache", "var/logs"):
            shutil.rmtree(plan.webroot / rel, ignore_errors=True)

        print("Writing nginx vhost")
        _write_nginx_vhost(plan)
        rc, out = _run(["nginx", "-t"], timeout_sec=30)
        if rc != 0:
            raise RuntimeError("nginx -t failed: " + out)
        _run(["systemctl", "reload", "nginx"], timeout_sec=30)

        if run_certbot:
            print("Running certbot")
            rc, out = _run(
                [
                    "certbot",
                    "--nginx",
                    "-d",
                    plan.domain,
                    "--non-interactive",
                    "--agree-tos",
                    "--redirect",
                    "--register-unsafely-without-email",
                ],
                timeout_sec=600,
            )
            if rc != 0:
                raise RuntimeError("certbot failed: " + out)

    inv = InstanceInventory(cfg.state_db_path)
    count = inv.rescan(cfg)
    print(f"Rescan complete: {count} instances")
    return {
        "status": "ok",
        "domain": plan.domain,
        "webroot": str(plan.webroot),
        "db_name": plan.db_name,
        "php_version": plan.php_version,
        "instances": count,
    }
