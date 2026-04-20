from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - py3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]

from mcd_agent.config import AgentConfig, resolve_mutable_config_path, upsert_section_values
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.models import DBConfig, MauticInstall
from mcd_agent.secret_store import SecretStore


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROFILE_ROW_ID = 1
_MYDUMPER_FLAG_CACHE: dict[tuple[str, str], bool] = {}


@dataclass(frozen=True)
class BackupResult:
    ok: bool
    message: str
    state_path: str
    backup_path: str | None = None
    duration_sec: int | None = None
    bytes_written: int | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt_local_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fmt_local_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _json_read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run(
    cmd: list[str],
    *,
    timeout_sec: int = 0,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if timeout_sec > 0:
        kwargs["timeout"] = timeout_sec
    if env is not None:
        kwargs["env"] = env
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        details = err or out or f"exit={proc.returncode}"
        raise RuntimeError(f"command failed: {' '.join(cmd)} :: {details}")
    return proc


def _mounted(path: Path) -> bool:
    p = str(path.resolve())
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == p:
            return True
    return False


def _unmount(path: Path, timeout_sec: int) -> None:
    if not _mounted(path):
        return
    try:
        _run(["fusermount", "-u", str(path)], timeout_sec=timeout_sec, check=True)
    except Exception:
        _run(["umount", "-l", str(path)], timeout_sec=timeout_sec, check=False)


def _format_remote_dir(root_dir: str, instance_name: str) -> str:
    base = root_dir.strip().strip("/")
    name = instance_name.strip().strip("/")
    if not base:
        return name
    return f"{base}/{name}"


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    _json_write(path / ".mcd-backup.json", payload)


def _verify_dump_dir(path: Path) -> tuple[bool, str, int]:
    if not path.exists() or not path.is_dir():
        return False, "dump directory missing", 0
    entries = list(path.iterdir())
    if not entries:
        return False, "dump directory is empty", 0
    metadata = [x for x in entries if x.name.startswith("metadata")]
    if not metadata:
        return False, "metadata file not found", 0
    data_files = [x for x in entries if x.is_file() and (x.name.endswith(".sql") or ".sql." in x.name)]
    if not data_files:
        return False, "no SQL chunk files found", 0
    total = 0
    for f in entries:
        if f.is_file():
            total += int(f.stat().st_size)
    if total <= 0:
        return False, "total dump size is zero", 0
    return True, "ok", total


def _storage_usage(path: Path) -> dict[str, Any] | None:
    try:
        du = shutil.disk_usage(path)
    except Exception:
        return None
    total = int(getattr(du, "total", 0) or 0)
    used = int(getattr(du, "used", 0) or 0)
    free = int(getattr(du, "free", 0) or 0)
    used_pct = (float(used) / float(total) * 100.0) if total > 0 else 0.0
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_pct": round(used_pct, 2),
        "checked_at": _utc_now_iso(),
    }


def _archive_files(cfg: AgentConfig, out_dir: Path) -> None:
    if not cfg.backup_archive_enabled:
        return
    src = [p for p in cfg.backup_archive_paths if Path(p).exists()]
    if not src:
        return
    target = out_dir / cfg.backup_archive_name
    cmd = ["tar", "-czf", str(target)] + src
    _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)


def _prune_by_copies(parent: Path, keep: int) -> list[str]:
    removed: list[str] = []
    if keep <= 0 or not parent.exists():
        return removed
    candidates = [x for x in parent.iterdir() if x.is_dir() and _DATE_RE.match(x.name)]
    candidates.sort(key=lambda x: x.name, reverse=True)
    for old in candidates[keep:]:
        subprocess.run(["rm", "-rf", str(old)], check=False)
        removed.append(old.name)
    return removed


def _cleanup_incomplete_dirs(parent: Path) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    failed: list[str] = []
    if not parent.exists():
        return removed, failed
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith(".incomplete-"):
            continue
        try:
            shutil.rmtree(child)
            removed.append(child.name)
        except Exception:
            failed.append(child.name)
    return removed, failed


def _list_instances(cfg: AgentConfig, *, force_rescan: bool = False) -> list[MauticInstall]:
    inv = InstanceInventory(cfg.state_db_path)
    if force_rescan:
        inv.rescan(cfg)
    else:
        ensure_seeded(inv, cfg)
    return inv.list_instances()


def _host_name(cfg: AgentConfig) -> str:
    raw = (cfg.backup_host_name or "").strip()
    if raw:
        return raw
    return socket.gethostname().strip() or "host"


def _host_slug(cfg: AgentConfig) -> str:
    name = _host_name(cfg)
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "host"


def _lock_path(cfg: AgentConfig) -> Path:
    return Path(cfg.backup_lock_dir) / f"backup-{_host_slug(cfg)}.lock"


def backup_lock_active(config: AgentConfig) -> bool:
    """Return True when host backup/restore lock is currently held."""
    cfg = _effective_cfg(config)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("w", encoding="utf-8")
    locked = False
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        return False
    except BlockingIOError:
        return True
    finally:
        if locked:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            lock_fh.close()
        except Exception:
            pass


def _state_path(cfg: AgentConfig) -> Path:
    return Path(cfg.backup_state_dir) / f"host-{_host_slug(cfg)}.json"


def _profile_store_conn(cfg: AgentConfig) -> sqlite3.Connection:
    Path(cfg.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg.state_db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_profile (
          id INTEGER PRIMARY KEY CHECK(id=1),
          payload_enc TEXT NOT NULL,
          updated_at REAL NOT NULL
        )
        """
    )
    con.commit()
    return con


def _profile_crypto(cfg: AgentConfig) -> SecretStore:
    return SecretStore(key_path=cfg.backup_secrets_key_path, env_master_key="MCD_BACKUP_MASTER_KEY")


def backup_profile_get(cfg: AgentConfig) -> dict[str, Any]:
    con = _profile_store_conn(cfg)
    try:
        row = con.execute("SELECT payload_enc FROM backup_profile WHERE id=?", (_PROFILE_ROW_ID,)).fetchone()
        if row is None:
            return {}
        token = str(row["payload_enc"] or "").strip()
        if not token:
            return {}
        plain = _profile_crypto(cfg).decrypt(token)
        payload = json.loads(plain)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    finally:
        con.close()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[index]
        else:
            out[k] = v
    return out


def _profile_storage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    src = payload.get("storage")
    if not isinstance(src, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("host", "user", "remote_path", "key_file", "password"):
        if key in src:
            out[key] = str(src.get(key) or "").strip() if key != "password" else str(src.get(key) or "")
    if "port" in src:
        try:
            out["port"] = int(src.get("port")) if src.get("port") is not None else 22
        except Exception:
            pass
    return out


def _profile_mysql_payload(payload: dict[str, Any]) -> dict[str, Any]:
    src = payload.get("mysql")
    if not isinstance(src, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("host", "user", "password", "database"):
        if key in src:
            out[key] = str(src.get(key) or "").strip() if key != "password" else str(src.get(key) or "")
    if "port" in src:
        try:
            out["port"] = int(src.get("port")) if src.get("port") is not None else 3306
        except Exception:
            pass
    return out


def _profile_archive_payload(payload: dict[str, Any]) -> dict[str, Any]:
    src = payload.get("archive")
    if not isinstance(src, dict):
        return {}
    out: dict[str, Any] = {}
    if "enabled" in src:
        out["enabled"] = bool(src.get("enabled"))
    if "name" in src:
        out["name"] = str(src.get("name") or "").strip()
    if "paths" in src:
        raw = src.get("paths")
        if isinstance(raw, list):
            out["paths"] = [str(x).strip() for x in raw if str(x).strip()]
    return out


def _sync_profile_payload_to_config(cfg: AgentConfig, payload: dict[str, Any]) -> bool:
    changed = False
    storage = _profile_storage_payload(payload)
    if storage:
        _, c = upsert_section_values(cfg.config_file_path, "backup.storage", storage)
        changed = changed or c
    mysql = _profile_mysql_payload(payload)
    if mysql:
        _, c = upsert_section_values(cfg.config_file_path, "backup.mysql", mysql)
        changed = changed or c
    archive = _profile_archive_payload(payload)
    if archive:
        _, c = upsert_section_values(cfg.config_file_path, "backup.archive", archive)
        changed = changed or c
    return changed


def _config_profile_payload(cfg: AgentConfig) -> dict[str, Any]:
    """
    Read explicit backup profile fragments from mutable config file.
    Only sections present in text config are returned.
    """
    p = resolve_mutable_config_path(cfg.config_file_path)
    if not p.exists():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    backup = raw.get("backup")
    if not isinstance(backup, dict):
        return {}
    out: dict[str, Any] = {}
    storage = _profile_storage_payload({"storage": backup.get("storage", {})})
    if storage:
        out["storage"] = storage
    mysql = _profile_mysql_payload({"mysql": backup.get("mysql", {})})
    if mysql:
        out["mysql"] = mysql
    archive = _profile_archive_payload({"archive": backup.get("archive", {})})
    if archive:
        out["archive"] = archive
    return out


def backup_profile_sync_from_config(cfg: AgentConfig) -> dict[str, Any]:
    """
    Sync explicit [backup.*] values from mutable text config into profile DB.
    This keeps hidden runtime profile in DB aligned with operator edits.
    """
    from_cfg = _config_profile_payload(cfg)
    if not from_cfg:
        return {"status": "skipped", "reason": "no_backup_profile_sections"}
    current = backup_profile_get(cfg)
    merged = _deep_merge(current, from_cfg)
    if merged == current:
        return {"status": "ok", "changed": False, "keys": []}
    backup_profile_set(cfg, from_cfg, merge=True, sync_config=False)
    return {"status": "ok", "changed": True, "keys": sorted(from_cfg.keys())}


def backup_profile_set(
    cfg: AgentConfig,
    payload: dict[str, Any],
    *,
    merge: bool = True,
    sync_config: bool = True,
) -> dict[str, Any]:
    current = backup_profile_get(cfg) if merge else {}
    merged = _deep_merge(current, payload)
    token = _profile_crypto(cfg).encrypt(json.dumps(merged, ensure_ascii=True, separators=(",", ":")))
    con = _profile_store_conn(cfg)
    try:
        con.execute(
            """
            INSERT INTO backup_profile(id, payload_enc, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET payload_enc=excluded.payload_enc, updated_at=excluded.updated_at
            """,
            (_PROFILE_ROW_ID, token, time.time()),
        )
        con.commit()
    finally:
        con.close()
    if sync_config:
        try:
            _sync_profile_payload_to_config(cfg, merged)
        except Exception:
            # Non-fatal: profile DB is authoritative for backup runtime.
            pass
    return merged


def _mask_value(v: str | None) -> str:
    if not v:
        return ""
    if len(v) <= 3:
        return "***"
    return v[:1] + "***" + v[-1:]


def backup_profile_masked(cfg: AgentConfig) -> dict[str, Any]:
    p = backup_profile_get(cfg)
    out = json.loads(json.dumps(p, ensure_ascii=True)) if p else {}
    storage = out.get("storage")
    if isinstance(storage, dict) and storage.get("password") is not None:
        storage["password"] = _mask_value(str(storage.get("password") or ""))
    mysql = out.get("mysql")
    if isinstance(mysql, dict) and mysql.get("password") is not None:
        mysql["password"] = _mask_value(str(mysql.get("password") or ""))
    return out


def _effective_cfg(cfg: AgentConfig) -> AgentConfig:
    p = backup_profile_get(cfg)
    if not p:
        return cfg
    storage = p.get("storage") if isinstance(p.get("storage"), dict) else {}
    mysql = p.get("mysql") if isinstance(p.get("mysql"), dict) else {}
    archive = p.get("archive") if isinstance(p.get("archive"), dict) else {}
    out = cfg
    if storage:
        out = replace(
            out,
            backup_ssh_host=str(storage.get("host")).strip() if storage.get("host") else out.backup_ssh_host,
            backup_ssh_port=int(storage.get("port")) if storage.get("port") is not None else out.backup_ssh_port,
            backup_ssh_user=str(storage.get("user")).strip() if storage.get("user") else out.backup_ssh_user,
            backup_ssh_remote_path=str(storage.get("remote_path")).strip() if storage.get("remote_path") else out.backup_ssh_remote_path,
            backup_ssh_key_file=str(storage.get("key_file")).strip() if storage.get("key_file") else out.backup_ssh_key_file,
            backup_ssh_password=str(storage.get("password")) if storage.get("password") else out.backup_ssh_password,
        )
    if mysql:
        out = replace(
            out,
            backup_mysql_host=str(mysql.get("host")).strip() if mysql.get("host") else out.backup_mysql_host,
            backup_mysql_port=int(mysql.get("port")) if mysql.get("port") is not None else out.backup_mysql_port,
            backup_mysql_user=str(mysql.get("user")).strip() if mysql.get("user") else out.backup_mysql_user,
            backup_mysql_password=str(mysql.get("password")) if mysql.get("password") else out.backup_mysql_password,
            backup_mysql_database=str(mysql.get("database")).strip() if mysql.get("database") else out.backup_mysql_database,
        )
    if archive:
        out = replace(
            out,
            backup_archive_enabled=bool(archive.get("enabled")) if archive.get("enabled") is not None else out.backup_archive_enabled,
            backup_archive_name=str(archive.get("name")).strip() if archive.get("name") else out.backup_archive_name,
            backup_archive_paths=[str(x) for x in archive.get("paths", []) if str(x).strip()]
            if isinstance(archive.get("paths"), list)
            else out.backup_archive_paths,
        )
    return out


def _validate_cfg(cfg: AgentConfig) -> None:
    if not cfg.backup_enabled:
        raise RuntimeError("backup is disabled in config ([backup].enabled=false)")
    if not cfg.backup_ssh_host or not cfg.backup_ssh_user:
        raise RuntimeError("backup storage is not configured ([backup.storage].host/user)")
    if not cfg.backup_ssh_key_file and not cfg.backup_ssh_password:
        raise RuntimeError("backup storage auth is not configured (key_file or password required)")


def _mount(cfg: AgentConfig, mount_path: Path) -> None:
    mount_path.mkdir(parents=True, exist_ok=True)
    if _mounted(mount_path):
        _unmount(mount_path, cfg.backup_unmount_timeout_sec)

    opts = [
        "reconnect",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "StrictHostKeyChecking=accept-new",
        f"port={cfg.backup_ssh_port}",
    ]
    if cfg.backup_ssh_key_file:
        opts.append(f"IdentityFile={cfg.backup_ssh_key_file}")
    if cfg.backup_ssh_password:
        opts.append("password_stdin")

    remote = f"{cfg.backup_ssh_user}@{cfg.backup_ssh_host}:{cfg.backup_ssh_remote_path}"
    cmd = ["sshfs", remote, str(mount_path), "-o", ",".join(opts)]
    if cfg.backup_ssh_password:
        proc = _run(
            cmd,
            timeout_sec=cfg.backup_mount_timeout_sec,
            input_text=cfg.backup_ssh_password + "\n",
            check=True,
        )
    else:
        proc = _run(cmd, timeout_sec=cfg.backup_mount_timeout_sec, check=True)
    if proc.returncode != 0 or not _mounted(mount_path):
        raise RuntimeError("sshfs mount did not appear in /proc/mounts")


def _mysql_defaults_file(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
) -> tempfile.NamedTemporaryFile:
    tf = tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8")
    tf.write("[client]\n")
    tf.write(f"host={host}\n")
    tf.write(f"port={port}\n")
    tf.write(f"user={user}\n")
    tf.write(f"password={password}\n")
    tf.flush()
    tf.close()
    try:
        os.chmod(tf.name, 0o600)
    except Exception:
        pass
    return tf


def _effective_db_for_instance(cfg: AgentConfig, inst: MauticInstall) -> DBConfig:
    if not inst.db:
        raise RuntimeError("Instance DB credentials are not available in inventory")
    if cfg.backup_mysql_user and cfg.backup_mysql_password:
        return DBConfig(
            host=cfg.backup_mysql_host or inst.db.host,
            port=int(cfg.backup_mysql_port or inst.db.port),
            name=cfg.backup_mysql_database or inst.db.name,
            user=cfg.backup_mysql_user,
            password=cfg.backup_mysql_password,
            table_prefix=inst.db.table_prefix,
        )
    return inst.db


def _mydumper_supports_flag(bin_path: str, flag: str) -> bool:
    key = (bin_path, flag)
    if key in _MYDUMPER_FLAG_CACHE:
        return _MYDUMPER_FLAG_CACHE[key]
    try:
        proc = subprocess.run(
            [bin_path, "--help"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        supported = flag in text
    except Exception:
        supported = False
    _MYDUMPER_FLAG_CACHE[key] = supported
    return supported


def _priority_prefix(cfg: AgentConfig) -> list[str]:
    prefix: list[str] = []
    if cfg.backup_mydumper_use_ionice and shutil.which("ionice"):
        ionice_class = max(1, min(3, int(cfg.backup_mydumper_ionice_class)))
        ionice_level = max(0, min(7, int(cfg.backup_mydumper_ionice_level)))
        prefix += ["ionice", "-c", str(ionice_class)]
        if ionice_class in {1, 2}:
            prefix += ["-n", str(ionice_level)]
    if cfg.backup_mydumper_use_nice and shutil.which("nice"):
        nice_level = max(-20, min(19, int(cfg.backup_mydumper_nice_level)))
        prefix += ["nice", "-n", str(nice_level)]
    return prefix


def _effective_long_query_guard(value: int) -> int:
    # Operator value <= 0 is treated as "disable guard".
    # mydumper interprets 0 as "abort if any query runs >0s", so use a very
    # large guard to emulate disabled behavior safely.
    return 2_147_483_647 if int(value) <= 0 else int(value)


def _build_mydumper_cmd(cfg: AgentConfig, db: DBConfig, output_dir: Path, defaults_file: str) -> list[str]:
    extra_args = _effective_mydumper_extra_args(cfg, cfg.backup_mydumper_extra_args)
    cmd = _priority_prefix(cfg) + [
        cfg.backup_mydumper_bin,
        f"--defaults-file={defaults_file}",
        "-B",
        db.name,
        "-o",
        str(output_dir),
        "--threads",
        str(cfg.backup_mydumper_threads),
        "--verbose",
        str(cfg.backup_mydumper_verbose),
    ]
    cmd += ["--long-query-guard", str(_effective_long_query_guard(cfg.backup_mydumper_long_query_guard))]
    if cfg.backup_mydumper_kill_long_queries:
        cmd.append("--kill-long-queries")
    if cfg.backup_mydumper_compress:
        cmd.append("--compress")
    cmd += extra_args
    return cmd


def _effective_mydumper_extra_args(cfg: AgentConfig, extra_args: list[str]) -> list[str]:
    # Keep lock behavior soft by default:
    # - prefer trx snapshot mode for InnoDB;
    # - avoid forcing hard lock modes (FTWRL/LOCK_ALL).
    # Operators can still override explicitly in extra_args.
    out = [str(x).strip() for x in extra_args if str(x).strip()]
    has_trx_tables = any(a == "--trx-tables" or a.startswith("--trx-tables=") for a in out)
    has_trx_consistency_only = any(
        a == "--trx-consistency-only" or a.startswith("--trx-consistency-only=")
        for a in out
    )
    has_lock_mode = any(a.startswith("--sync-thread-lock-mode") for a in out)
    if not has_lock_mode and _mydumper_supports_flag(cfg.backup_mydumper_bin, "--sync-thread-lock-mode"):
        out.append("--sync-thread-lock-mode=AUTO")
    if not has_trx_tables and not has_trx_consistency_only:
        if _mydumper_supports_flag(cfg.backup_mydumper_bin, "--trx-tables"):
            out.append("--trx-tables")
        else:
            out.append("--trx-consistency-only")
    return out


def _is_long_query_guard_abort(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "long-query-guard" not in msg:
        return False
    return (
        "queries in processlist running longer than" in msg
        or "aborting dump" in msg
    )


def _run_mydumper(cfg: AgentConfig, db: DBConfig, output_dir: Path) -> None:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    primary_exc: Exception | None = None
    try:
        cmd = _build_mydumper_cmd(cfg, db, output_dir, defaults.name)
        _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
        return
    except Exception as e:
        primary_exc = e
        # Some mydumper versions return non-zero even when dump files are complete
        # (e.g. global lock warnings with --trx-consistency-only).
        ok, _, _ = _verify_dump_dir(output_dir)
        if ok:
            return
        if _is_long_query_guard_abort(e):
            # Fallback safety net: when long-query-guard trips, rerun with guard
            # effectively disabled so dump can proceed on busy production DBs.
            no_guard_cfg = replace(cfg, backup_mydumper_long_query_guard=0)
            cmd_no_guard = _build_mydumper_cmd(no_guard_cfg, db, output_dir, defaults.name)
            try:
                _run(cmd_no_guard, timeout_sec=no_guard_cfg.backup_dump_timeout_sec, check=True)
                return
            except Exception:
                ok, _, _ = _verify_dump_dir(output_dir)
                if ok:
                    return
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        extra_args = _effective_mydumper_extra_args(cfg, cfg.backup_mydumper_extra_args)
        guard_value = cfg.backup_mydumper_long_query_guard
        if _is_long_query_guard_abort(e):
            guard_value = 0
        fallback = _priority_prefix(cfg) + [
            cfg.backup_mydumper_bin,
            "-u",
            db.user,
            "-h",
            db.host,
            "-P",
            str(db.port),
            "-B",
            db.name,
            "-o",
            str(output_dir),
            "--threads",
            str(cfg.backup_mydumper_threads),
            "--verbose",
            str(cfg.backup_mydumper_verbose),
        ]
        fallback += ["--long-query-guard", str(_effective_long_query_guard(guard_value))]
        if cfg.backup_mydumper_kill_long_queries:
            fallback.append("--kill-long-queries")
        if cfg.backup_mydumper_compress:
            fallback.append("--compress")
        fallback += extra_args
        try:
            _run(fallback, timeout_sec=cfg.backup_dump_timeout_sec, env=env, check=True)
            return
        except Exception:
            ok, _, _ = _verify_dump_dir(output_dir)
            if ok:
                return
            if primary_exc is not None:
                raise primary_exc
            raise
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _run_mysql_sql(cfg: AgentConfig, db: DBConfig, sql: str) -> None:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    try:
        _run(["mysql", f"--defaults-extra-file={defaults.name}", "-e", sql], timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    except Exception:
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        _run(
            ["mysql", "-h", db.host, "-P", str(db.port), "-u", db.user, "-e", sql],
            timeout_sec=cfg.backup_dump_timeout_sec,
            env=env,
            check=True,
        )
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _run_myloader(cfg: AgentConfig, db: DBConfig, dump_dir: Path) -> None:
    defaults = _mysql_defaults_file(host=db.host, port=db.port, user=db.user, password=db.password)
    try:
        cmd = [
            cfg.backup_myloader_bin,
            f"--defaults-file={defaults.name}",
            "-B",
            db.name,
            "-d",
            str(dump_dir),
            "--threads",
            str(cfg.backup_myloader_threads),
            "--overwrite-tables",
        ]
        _run(cmd, timeout_sec=cfg.backup_dump_timeout_sec, check=True)
    except Exception:
        env = dict(os.environ)
        env["MYSQL_PWD"] = db.password
        fallback = [
            cfg.backup_myloader_bin,
            "-h",
            db.host,
            "-P",
            str(db.port),
            "-u",
            db.user,
            "-B",
            db.name,
            "-d",
            str(dump_dir),
            "--threads",
            str(cfg.backup_myloader_threads),
            "--overwrite-tables",
        ]
        _run(fallback, timeout_sec=cfg.backup_dump_timeout_sec, env=env, check=True)
    finally:
        try:
            os.remove(defaults.name)
        except Exception:
            pass


def _candidate_db(dump_dir: Path, instances: list[MauticInstall], cfg: AgentConfig) -> DBConfig | None:
    name = dump_dir.name
    db_name = ""
    if "__" in name:
        db_name = name.split("__", 1)[1].strip()
    if cfg.backup_mysql_user and cfg.backup_mysql_password:
        host = cfg.backup_mysql_host or "localhost"
        port = int(cfg.backup_mysql_port or 3306)
        chosen = cfg.backup_mysql_database or db_name
        if not chosen:
            return None
        return DBConfig(
            host=host,
            port=port,
            name=chosen,
            user=cfg.backup_mysql_user,
            password=cfg.backup_mysql_password,
            table_prefix="",
        )

    for inst in instances:
        if not inst.db:
            continue
        if db_name and inst.db.name == db_name:
            return inst.db
    return None


def _resolve_restore_source(
    *,
    remote_parent: Path,
    date: str | None,
    path: str | None,
) -> Path:
    if path:
        p = Path(path)
        if p.exists() and p.is_dir():
            return p
        rel = remote_parent / path.strip().strip("/")
        if rel.exists() and rel.is_dir():
            return rel
        raise RuntimeError(f"restore source not found: {path}")

    if date:
        d = date.strip()
        if not _DATE_RE.match(d):
            raise RuntimeError("date must be YYYY-MM-DD")
        p = remote_parent / d
        if not p.exists() or not p.is_dir():
            raise RuntimeError(f"backup date not found: {d}")
        return p

    candidates = [x for x in remote_parent.iterdir() if x.is_dir() and _DATE_RE.match(x.name)]
    if not candidates:
        raise RuntimeError(f"no backups found in {remote_parent}")
    candidates.sort(key=lambda x: x.name, reverse=True)
    return candidates[0]


def _read_backup_marker(backup_dir: Path) -> dict[str, Any]:
    p = backup_dir / ".mcd-backup.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def backup_run(config: AgentConfig, root: str | None = None) -> BackupResult:
    cfg = _effective_cfg(config)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    start_monotonic = time.monotonic()
    started_ts = _utc_now_iso()
    try:
        _validate_cfg(cfg)
        instances = _list_instances(cfg)
        db_instances = [x for x in instances if x.db]
        if not db_instances:
            raise RuntimeError("No instances with DB credentials found in inventory")
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        fail_state = dict(state)
        history = fail_state.get("history", [])
        if not isinstance(history, list):
            history = []
        history = [
            {
                "ts": started_ts,
                "status": "failed",
                "duration_sec": duration,
                "error": str(e),
            }
        ] + history[:19]
        fail_state.update(
            {
                "host_name": _host_name(cfg),
                "selected_root": root or "",
                "last_run_at": started_ts,
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "job": "backup.run",
                "history": history,
            }
        )
        _json_write(state_path, fail_state)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)

    host_name = _host_name(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, host_name)
    date_dir = _fmt_local_date()
    tmp_dir = remote_parent / f".incomplete-{date_dir}-{_fmt_local_ts()}"
    final_dir = remote_parent / date_dir

    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(
            ok=False,
            message="backup is already running for this host",
            state_path=str(state_path),
        )

    run_state: dict[str, Any] = dict(state)
    history = state.get("history", [])
    if not isinstance(history, list):
        history = []
    run_state.update(
        {
            "host_name": host_name,
            "instances_total": len(instances),
            "instances_with_db": len(db_instances),
            "selected_root": root or "",
            "last_run_at": started_ts,
            "last_status": "running",
            "last_error": "",
            "last_success_at": state.get("last_success_at", ""),
            "last_backup_path": state.get("last_backup_path", ""),
            "job": "backup.run",
        }
    )
    _json_write(state_path, run_state)

    bytes_written = 0
    try:
        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        _removed_incomplete, failed_incomplete = _cleanup_incomplete_dirs(remote_parent)
        if failed_incomplete:
            raise RuntimeError(
                "failed to cleanup stale incomplete backup dirs: "
                + ", ".join(sorted(failed_incomplete))
            )
        _prune_by_copies(remote_parent, cfg.backup_retention_copies)
        if final_dir.exists():
            marker = _read_backup_marker(final_dir)
            if str(marker.get("status") or "").strip().lower() == "ok":
                duration = int(time.monotonic() - start_monotonic)
                storage_usage = _storage_usage(mount_path)
                existing_bytes: int | None
                try:
                    existing_bytes = int(marker.get("bytes_written")) if marker.get("bytes_written") is not None else None
                except Exception:
                    existing_bytes = None
                success_state = dict(run_state)
                hist_item = {
                    "ts": _utc_now_iso(),
                    "status": "ok_skip_existing",
                    "duration_sec": duration,
                    "backup_path": str(final_dir),
                    "bytes_written": existing_bytes,
                    "instances_with_db": len(db_instances),
                }
                history = [hist_item] + history[:19]
                success_state.update(
                    {
                        "last_status": "ok",
                        "last_error": "",
                        "last_success_at": _utc_now_iso(),
                        "last_duration_sec": duration,
                        "last_backup_path": str(final_dir),
                        "history": history,
                    }
                )
                if existing_bytes is not None:
                    success_state["last_bytes_written"] = existing_bytes
                if isinstance(storage_usage, dict):
                    success_state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
                    success_state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
                    success_state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
                    success_state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
                    success_state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or "")
                _json_write(state_path, success_state)
                return BackupResult(
                    ok=True,
                    message=f"backup already exists for today: {final_dir}",
                    state_path=str(state_path),
                    backup_path=str(final_dir),
                    duration_sec=duration,
                    bytes_written=existing_bytes,
                )
            raise RuntimeError(f"backup target already exists: {final_dir}")
        tmp_dir.mkdir(parents=True, exist_ok=False)

        _archive_files(cfg, tmp_dir)

        db_root = tmp_dir / "databases"
        db_root.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        dumped: list[dict[str, Any]] = []
        seen_db_keys: set[tuple[str, int, str, str]] = set()
        for inst in db_instances:
            assert inst.db is not None
            effective_db = _effective_db_for_instance(cfg, inst)
            key = (effective_db.host, effective_db.port, effective_db.user, effective_db.name)
            if key in seen_db_keys:
                continue
            seen_db_keys.add(key)
            db_dir = db_root / f"{inst.instance_uid}__{effective_db.name}"
            db_dir.mkdir(parents=True, exist_ok=False)
            _run_mydumper(cfg, effective_db, db_dir)
            ok, verify_msg, one_bytes = _verify_dump_dir(db_dir)
            if not ok:
                raise RuntimeError(f"backup verification failed for {inst.instance_uid}: {verify_msg}")
            total_bytes += one_bytes
            dumped.append(
                {
                    "instance_uid": inst.instance_uid,
                    "instance_name": inst.name,
                    "root": inst.root,
                    "database": effective_db.name,
                    "bytes": one_bytes,
                }
            )
        bytes_written = total_bytes

        os.replace(tmp_dir, final_dir)
        storage_usage = _storage_usage(mount_path)
        marker = {
            "status": "ok",
            "ts_utc": _utc_now_iso(),
            "host_name": host_name,
            "path": str(final_dir),
            "bytes_written": bytes_written,
            "instances_total": len(instances),
            "instances_with_db": len(db_instances),
            "dumped_instances": dumped,
            "mydumper_threads": cfg.backup_mydumper_threads,
        }
        if isinstance(storage_usage, dict):
            marker["storage"] = {
                "total_bytes": int(storage_usage.get("total_bytes") or 0),
                "used_bytes": int(storage_usage.get("used_bytes") or 0),
                "free_bytes": int(storage_usage.get("free_bytes") or 0),
                "used_pct": float(storage_usage.get("used_pct") or 0.0),
                "checked_at": str(storage_usage.get("checked_at") or ""),
            }
        _write_marker(final_dir, marker)

        duration = int(time.monotonic() - start_monotonic)
        success_state = dict(run_state)
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "ok",
            "duration_sec": duration,
            "backup_path": str(final_dir),
            "bytes_written": bytes_written,
            "instances_with_db": len(db_instances),
        }
        history = [hist_item] + history[:19]
        success_state.update(
            {
                "last_status": "ok",
                "last_error": "",
                "last_success_at": _utc_now_iso(),
                "last_duration_sec": duration,
                "last_backup_path": str(final_dir),
                "last_bytes_written": bytes_written,
                "history": history,
            }
        )
        if isinstance(storage_usage, dict):
            success_state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
            success_state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
            success_state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
            success_state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
            success_state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or "")
        _json_write(state_path, success_state)
        return BackupResult(
            ok=True,
            message=f"host backup completed: {final_dir}",
            state_path=str(state_path),
            backup_path=str(final_dir),
            duration_sec=duration,
            bytes_written=bytes_written,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        fail_state = dict(run_state)
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "failed",
            "duration_sec": duration,
            "error": str(e),
        }
        history = [hist_item] + history[:19]
        fail_state.update(
            {
                "last_status": "failed",
                "last_error": str(e),
                "last_duration_sec": duration,
                "history": history,
            }
        )
        _json_write(state_path, fail_state)
        if tmp_dir.exists():
            subprocess.run(["rm", "-rf", str(tmp_dir)], check=False)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path))
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def backup_restore(
    config: AgentConfig,
    *,
    root: str | None = None,
    date: str | None = None,
    path: str | None = None,
) -> BackupResult:
    _ = root
    cfg = _effective_cfg(config)
    _validate_cfg(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    host_name = _host_name(cfg)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, host_name)
    start_monotonic = time.monotonic()

    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return BackupResult(
            ok=False,
            message="backup/restore lock is busy on this host",
            state_path=str(state_path),
        )

    try:
        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        source_dir = _resolve_restore_source(remote_parent=remote_parent, date=date, path=path)
        marker = _read_backup_marker(source_dir)

        if cfg.backup_restore_apply_files:
            files_archive = source_dir / cfg.backup_archive_name
            if files_archive.exists():
                _run(["tar", "-xzf", str(files_archive), "-C", "/"], timeout_sec=cfg.backup_dump_timeout_sec, check=True)

        restored_dbs = 0
        if cfg.backup_restore_apply_databases:
            db_root = source_dir / "databases"
            if db_root.exists() and db_root.is_dir():
                instances = _list_instances(cfg, force_rescan=True)
                for dump_dir in sorted([x for x in db_root.iterdir() if x.is_dir()], key=lambda p: p.name):
                    db = _candidate_db(dump_dir, instances, cfg)
                    if db is None:
                        continue
                    _run_mysql_sql(cfg, db, f"CREATE DATABASE IF NOT EXISTS `{db.name}`")
                    _run_myloader(cfg, db, dump_dir)
                    restored_dbs += 1

        duration = int(time.monotonic() - start_monotonic)
        last = dict(state)
        history = last.get("history", [])
        if not isinstance(history, list):
            history = []
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "restore_ok",
            "duration_sec": duration,
            "backup_path": str(source_dir),
            "restored_databases": restored_dbs,
        }
        history = [hist_item] + history[:19]
        last.update(
            {
                "last_restore_at": _utc_now_iso(),
                "last_restore_status": "ok",
                "last_restore_error": "",
                "last_restore_path": str(source_dir),
                "last_restore_duration_sec": duration,
                "last_restore_databases": restored_dbs,
                "history": history,
            }
        )
        if marker:
            last["last_restore_marker"] = marker
        _json_write(state_path, last)
        return BackupResult(
            ok=True,
            message=f"restore completed: {source_dir}",
            state_path=str(state_path),
            backup_path=str(source_dir),
            duration_sec=duration,
        )
    except Exception as e:
        duration = int(time.monotonic() - start_monotonic)
        fail = dict(state)
        history = fail.get("history", [])
        if not isinstance(history, list):
            history = []
        hist_item = {
            "ts": _utc_now_iso(),
            "status": "restore_failed",
            "duration_sec": duration,
            "error": str(e),
        }
        history = [hist_item] + history[:19]
        fail.update(
            {
                "last_restore_at": _utc_now_iso(),
                "last_restore_status": "failed",
                "last_restore_error": str(e),
                "last_restore_duration_sec": duration,
                "history": history,
            }
        )
        _json_write(state_path, fail)
        return BackupResult(ok=False, message=str(e), state_path=str(state_path), duration_sec=duration)
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


def backup_storage_probe(config: AgentConfig, root: str | None = None) -> dict[str, Any]:
    _ = root
    cfg = _effective_cfg(config)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    lock_path = _lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    checked_at = _utc_now_iso()

    try:
        _validate_cfg(cfg)
    except Exception as e:
        state["last_storage_probe_at"] = checked_at
        state["last_storage_probe_status"] = "failed"
        state["last_storage_probe_error"] = str(e)
        _json_write(state_path, state)
        return {
            "ok": False,
            "status": "failed",
            "error": str(e),
            "state_path": str(state_path),
        }

    lock_fh = lock_path.open("w", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "ok": True,
                "status": "skipped",
                "reason": "backup_lock_busy",
                "state_path": str(state_path),
            }

        try:
            _mount(cfg, mount_path)
            storage_usage = _storage_usage(mount_path)
            if not isinstance(storage_usage, dict):
                raise RuntimeError("storage usage probe returned no data")
            state["last_storage_total_bytes"] = int(storage_usage.get("total_bytes") or 0)
            state["last_storage_used_bytes"] = int(storage_usage.get("used_bytes") or 0)
            state["last_storage_free_bytes"] = int(storage_usage.get("free_bytes") or 0)
            state["last_storage_used_pct"] = float(storage_usage.get("used_pct") or 0.0)
            state["last_storage_checked_at"] = str(storage_usage.get("checked_at") or checked_at)
            state["last_storage_probe_at"] = checked_at
            state["last_storage_probe_status"] = "ok"
            state["last_storage_probe_error"] = ""
            _json_write(state_path, state)
            return {
                "ok": True,
                "status": "ok",
                "state_path": str(state_path),
                "checked_at": state["last_storage_checked_at"],
            }
        except Exception as e:
            state["last_storage_probe_at"] = checked_at
            state["last_storage_probe_status"] = "failed"
            state["last_storage_probe_error"] = str(e)
            _json_write(state_path, state)
            return {
                "ok": False,
                "status": "failed",
                "error": str(e),
                "state_path": str(state_path),
            }
        finally:
            try:
                _unmount(mount_path, cfg.backup_unmount_timeout_sec)
            except Exception:
                pass
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fh.close()
        except Exception:
            pass


def backup_status(config: AgentConfig, root: str | None = None) -> dict[str, Any]:
    _ = root
    cfg = _effective_cfg(config)
    state_path = _state_path(cfg)
    state = _json_read(state_path)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    state.setdefault("host_name", _host_name(cfg))
    state["mounted"] = _mounted(mount_path)
    state["state_path"] = str(state_path)
    return state


def backup_state_for_push(config: AgentConfig) -> dict[str, Any]:
    st = backup_status(config)
    return {
        "last_run_at": st.get("last_run_at"),
        "last_success_at": st.get("last_success_at"),
        "last_status": st.get("last_status"),
        "last_error": st.get("last_error"),
        "last_backup_path": st.get("last_backup_path"),
        "last_duration_sec": st.get("last_duration_sec"),
        "last_bytes_written": st.get("last_bytes_written"),
        "last_restore_at": st.get("last_restore_at"),
        "last_restore_status": st.get("last_restore_status"),
        "last_restore_error": st.get("last_restore_error"),
        "last_restore_path": st.get("last_restore_path"),
        "last_storage_checked_at": st.get("last_storage_checked_at"),
        "last_storage_total_bytes": st.get("last_storage_total_bytes"),
        "last_storage_used_bytes": st.get("last_storage_used_bytes"),
        "last_storage_free_bytes": st.get("last_storage_free_bytes"),
        "last_storage_used_pct": st.get("last_storage_used_pct"),
        "last_storage_probe_at": st.get("last_storage_probe_at"),
        "last_storage_probe_status": st.get("last_storage_probe_status"),
        "last_storage_probe_error": st.get("last_storage_probe_error"),
    }


def backup_profile_for_push(config: AgentConfig) -> dict[str, Any]:
    cfg = _effective_cfg(config)
    if not cfg.backup_enabled:
        return {"enabled": False}
    profile = backup_profile_get(config)
    payload: dict[str, Any] = {
        "enabled": bool(cfg.backup_enabled),
        "storage": {
            "host": cfg.backup_ssh_host,
            "port": int(cfg.backup_ssh_port),
            "user": cfg.backup_ssh_user,
            "remote_path": cfg.backup_ssh_remote_path,
            "key_file": cfg.backup_ssh_key_file or "",
            "password": cfg.backup_ssh_password or "",
        },
        "mysql": {
            "host": cfg.backup_mysql_host or "",
            "port": int(cfg.backup_mysql_port or 0),
            "user": cfg.backup_mysql_user or "",
            "password": cfg.backup_mysql_password or "",
            "database": cfg.backup_mysql_database or "",
        },
        "archive": {
            "enabled": bool(cfg.backup_archive_enabled),
            "name": cfg.backup_archive_name,
            "paths": list(cfg.backup_archive_paths),
        },
        "schedule": {
            "enabled": bool(cfg.backup_schedule_enabled),
            "interval_sec": int(cfg.backup_schedule_interval_sec),
            "quiet_hour": int(cfg.backup_schedule_quiet_hour),
            "quiet_window_min": int(cfg.backup_schedule_quiet_window_min),
            "pre_pause_sec": int(cfg.backup_schedule_pre_pause_sec),
        },
        "source": "profile_db" if profile else "config",
    }
    return payload


def backup_prune(config: AgentConfig, root: str | None = None) -> BackupResult:
    _ = root
    cfg = _effective_cfg(config)
    _validate_cfg(cfg)
    mount_path = Path(cfg.backup_mount_base_dir) / _host_slug(cfg)
    remote_parent = mount_path / _format_remote_dir(cfg.backup_remote_root_dir, _host_name(cfg))
    removed: list[str] = []
    try:
        _mount(cfg, mount_path)
        remote_parent.mkdir(parents=True, exist_ok=True)
        removed = _prune_by_copies(remote_parent, cfg.backup_retention_copies)
        return BackupResult(
            ok=True,
            message=f"prune done, removed={len(removed)}",
            state_path=str(_state_path(cfg)),
        )
    except Exception as e:
        return BackupResult(
            ok=False,
            message=str(e),
            state_path=str(_state_path(cfg)),
        )
    finally:
        try:
            _unmount(mount_path, cfg.backup_unmount_timeout_sec)
        except Exception:
            pass
