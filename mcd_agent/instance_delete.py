from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import urlparse

from mcd_agent.config import AgentConfig
from mcd_agent.localphp import parse_local_php


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")
_LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
_NGINX_DIRS = (Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/sites-available"))


@dataclass
class InstanceDeletePlan:
    root: Path
    local_php: Path | None
    domains: list[str]
    nginx_paths: list[Path]
    db_name: str
    db_host: str
    db_port: str
    db_user: str
    db_password: str
    delete_files: bool
    delete_vhost: bool
    delete_db: bool


def _run(
    args: list[str],
    *,
    timeout_sec: int = 120,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
        env=env,
    )
    return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()


def _mysql_bin() -> str:
    for name in ("mariadb", "mysql"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("mysql/mariadb client is missing")


def _mysql_exec(
    sql: str,
    *,
    timeout_sec: int = 120,
    host: str = "",
    port: str = "",
    user: str = "",
    password: str = "",
) -> str:
    args = [_mysql_bin(), "-N", "-B"]
    clean_host = str(host or "").strip()
    clean_port = str(port or "").strip()
    clean_user = str(user or "").strip()
    if clean_user:
        args.extend(["-u", clean_user])
    if clean_host and clean_host != "localhost":
        args.extend(["-h", clean_host])
    if clean_port and clean_host and clean_host != "localhost":
        args.extend(["-P", clean_port])
    args.extend(["-e", sql])
    env = None
    if password:
        env = dict(os.environ)
        env["MYSQL_PWD"] = str(password)
    rc, out = _run(args, timeout_sec=timeout_sec, env=env)
    if rc != 0:
        raise RuntimeError(out or "mysql command failed")
    return out


def _quote_ident(value: str) -> str:
    return "`" + str(value).replace("`", "``") + "`"


def _clean_domain(raw: str) -> str:
    domain = str(raw or "").strip().lower().rstrip(".")
    if not domain:
        return ""
    if not _DOMAIN_RE.match(domain):
        raise RuntimeError(f"invalid domain: {raw}")
    return domain


def _domain_from_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) else "https://" + text)
    host = str(parsed.hostname or "").strip().lower()
    return _clean_domain(host) if host else ""


def _safe_root(raw: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("--root is required")
    root = Path(text)
    if not root.is_absolute():
        raise RuntimeError("root must be an absolute path")
    resolved = root.resolve(strict=False)
    var_www = Path("/var/www").resolve(strict=False)
    if resolved == Path("/") or resolved == var_www or var_www not in resolved.parents:
        raise RuntimeError(f"unsafe root outside /var/www: {resolved}")
    rel_parts = resolved.relative_to(var_www).parts
    if not rel_parts:
        raise RuntimeError(f"unsafe root is too broad: {resolved}")
    if len(rel_parts) == 1 and not ((resolved / "config" / "local.php").exists() or (resolved / "app" / "config" / "local.php").exists()):
        raise RuntimeError(f"unsafe one-level root without local.php: {resolved}")
    return resolved


def _local_php_path(root: Path) -> Path | None:
    for rel in ("config/local.php", "app/config/local.php"):
        path = root / rel
        if path.exists() and path.is_file():
            return path
    return None


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _safe_nginx_child(path: Path) -> bool:
    try:
        parent = path.parent.resolve(strict=False)
    except Exception:
        return False
    return parent in {d.resolve(strict=False) for d in _NGINX_DIRS}


def _read_nginx_text(path: Path) -> str:
    target = path
    if path.is_symlink():
        try:
            target = path.resolve(strict=True)
        except Exception:
            return ""
    if not target.exists() or not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _nginx_candidates(root: Path, domains: list[str]) -> list[Path]:
    wanted: list[Path] = []
    domain_set = {d.lower() for d in domains if d}
    root_text = str(root)
    for domain in sorted(domain_set):
        for base in _NGINX_DIRS:
            wanted.append(base / f"{domain}.conf")

    for base in _NGINX_DIRS:
        if not base.exists() or not base.is_dir():
            continue
        for item in base.iterdir():
            if not item.is_file() and not item.is_symlink():
                continue
            text = _read_nginx_text(item)
            if not text:
                continue
            lower = text.lower()
            matched_domain = any(d in lower for d in domain_set)
            matched_root = root_text in text
            if matched_domain or matched_root:
                wanted.append(item)
                if item.is_symlink():
                    try:
                        target = item.resolve(strict=True)
                        if target.parent.resolve(strict=False) == Path("/etc/nginx/sites-available").resolve(strict=False):
                            wanted.append(target)
                    except Exception:
                        pass
    return [p for p in _dedupe_paths(wanted) if _safe_nginx_child(p)]


def build_delete_plan(
    *,
    root: str,
    domains: list[str] | None = None,
    db_name: str | None = None,
    delete_files: bool = False,
    delete_vhost: bool = False,
    delete_db: bool = False,
) -> InstanceDeletePlan:
    target_root = _safe_root(root)
    local_php = _local_php_path(target_root)
    local_cfg: dict[str, str] = {}
    if local_php is not None:
        local_cfg = parse_local_php(str(local_php))

    cleaned_domains: list[str] = []
    seen_domains: set[str] = set()
    for raw_domain in list(domains or []):
        d = _clean_domain(raw_domain)
        if d and d not in seen_domains:
            seen_domains.add(d)
            cleaned_domains.append(d)
    site_domain = _domain_from_url(local_cfg.get("site_url", ""))
    if site_domain and site_domain not in seen_domains:
        cleaned_domains.append(site_domain)

    final_db_name = str(db_name or local_cfg.get("db_name", "") or "").strip()
    db_host = str(local_cfg.get("db_host", "") or "").strip().lower()
    db_port = str(local_cfg.get("db_port", "") or "").strip()
    db_user = str(local_cfg.get("db_user", "") or "").strip()
    db_password = str(local_cfg.get("db_password", "") or "")
    if delete_db:
        if not final_db_name:
            raise RuntimeError("database name is unavailable; local.php is missing or incomplete")
        if not _DB_NAME_RE.match(final_db_name):
            raise RuntimeError(f"unsafe database name: {final_db_name}")
        if db_host not in _LOCAL_DB_HOSTS:
            raise RuntimeError(f"refusing to drop non-local database host: {db_host}")
        if not db_user:
            raise RuntimeError("database user is unavailable; local.php is missing or incomplete")

    nginx_paths = _nginx_candidates(target_root, cleaned_domains) if delete_vhost else []
    return InstanceDeletePlan(
        root=target_root,
        local_php=local_php,
        domains=cleaned_domains,
        nginx_paths=nginx_paths,
        db_name=final_db_name,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        delete_files=bool(delete_files),
        delete_vhost=bool(delete_vhost),
        delete_db=bool(delete_db),
    )


def _plan_public(plan: InstanceDeletePlan) -> dict[str, Any]:
    return {
        "root": str(plan.root),
        "local_php": str(plan.local_php) if plan.local_php else "",
        "domains": list(plan.domains),
        "nginx_paths": [str(p) for p in plan.nginx_paths],
        "db_name": plan.db_name,
        "db_host": plan.db_host or "localhost",
        "db_port": plan.db_port or "3306",
        "db_user": plan.db_user,
        "delete_files": plan.delete_files,
        "delete_vhost": plan.delete_vhost,
        "delete_db": plan.delete_db,
    }


def delete_instance_artifacts(
    cfg: AgentConfig,
    *,
    root: str,
    domains: list[str] | None = None,
    db_name: str | None = None,
    delete_files: bool = False,
    delete_vhost: bool = False,
    delete_db: bool = False,
    yes: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not (delete_files or delete_vhost or delete_db):
        raise RuntimeError("select at least one deletion target")
    plan = build_delete_plan(
        root=root,
        domains=domains,
        db_name=db_name,
        delete_files=delete_files,
        delete_vhost=delete_vhost,
        delete_db=delete_db,
    )
    result: dict[str, Any] = {
        "status": "planned" if dry_run else "ok",
        "plan": _plan_public(plan),
        "deleted": {"files": False, "vhost": [], "db": False},
        "warnings": [],
    }
    if dry_run:
        return result
    if not yes:
        raise RuntimeError("--yes is required")
    if os.geteuid() != 0:
        raise RuntimeError("must run as root")

    if plan.delete_db:
        print(f"Dropping database {plan.db_name}")
        _mysql_exec(
            f"DROP DATABASE IF EXISTS {_quote_ident(plan.db_name)}",
            timeout_sec=300,
            host=plan.db_host,
            port=plan.db_port,
            user=plan.db_user,
            password=plan.db_password,
        )
        result["deleted"]["db"] = True

    if plan.delete_vhost:
        print("Deleting nginx vhost")
        if not plan.nginx_paths:
            result["warnings"].append("no matching nginx vhost files found")
        for path in plan.nginx_paths:
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
                    result["deleted"]["vhost"].append(str(path))
            except Exception as exc:
                raise RuntimeError(f"failed to remove nginx path {path}: {exc}") from exc
        if plan.nginx_paths:
            rc, out = _run(["nginx", "-t"], timeout_sec=30)
            if rc != 0:
                raise RuntimeError("nginx -t failed after vhost removal: " + out)
            _run(["systemctl", "reload", "nginx"], timeout_sec=30)

    if plan.delete_files:
        print(f"Deleting files {plan.root}")
        if plan.root.is_symlink():
            raise RuntimeError(f"refusing to delete symlink root: {plan.root}")
        if not plan.root.exists():
            result["warnings"].append(f"root already absent: {plan.root}")
        else:
            shutil.rmtree(plan.root)
            result["deleted"]["files"] = True

    try:
        from mcd_agent.inventory import InstanceInventory

        count = InstanceInventory(cfg.state_db_path).rescan(cfg)
        result["instances"] = count
        print(f"Rescan complete: {count} instances")
    except Exception as exc:
        result["warnings"].append(f"inventory rescan failed: {exc}")
    if result["warnings"]:
        result["status"] = "warning"
    return result
