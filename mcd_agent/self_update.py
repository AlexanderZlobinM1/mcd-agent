from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from mcd_agent import __version__
from mcd_agent.backup import backup_lock_active
from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def _semver(v: str) -> tuple[int, int, int]:
    nums = [x for x in "".join(ch if ch.isdigit() else "." for ch in (v or "")).split(".") if x.isdigit()]
    while len(nums) < 3:
        nums.append("0")
    return (int(nums[0]), int(nums[1]), int(nums[2]))


def _state_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "mcd-self-update.json"


def _lock_path(cfg: AgentConfig) -> Path:
    return Path(cfg.state_db_path).parent / "mcd-self-update.lock"


def _read_state(cfg: AgentConfig) -> dict[str, Any]:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        return {}
    return {}


def _write_state(cfg: AgentConfig, payload: dict[str, Any]) -> None:
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _post_json(url: str, payload: dict[str, Any], token: str | None, timeout_sec: int = 10) -> dict[str, Any]:
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


def _update_policy(cfg: AgentConfig) -> str:
    p = (cfg.mcd_update_policy or "").strip().lower()
    if p in {"off", "lts", "approved", "test", "cluster"}:
        return p
    ch = (cfg.mcd_update_channel or "").strip().lower()
    if ch in {"stable", "approved"}:
        return "approved"
    if ch in {"rc", "test"}:
        return "test"
    if ch == "lts":
        return "lts"
    if ch == "cluster":
        return "cluster"
    if ch == "off":
        return "off"
    return "approved"


def check_with_mcc(cfg: AgentConfig, *, auto_update_enabled: bool) -> dict[str, Any]:
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
        "auto_update_enabled": bool(auto_update_enabled),
        "update_policy": _update_policy(cfg),
        "allow_test_build": bool(cfg.mcd_update_allow_test_build),
        "config_sha256": cfg.config_sha256,
    }
    url = base + "/api/v1/agent/update/check"
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


def release_session(cfg: AgentConfig, session_id: str, *, result_status: str, result_message: str, new_version: str) -> None:
    base = _api_base(cfg)
    if not base or not session_id:
        return
    ident = resolve_agent_identity(cfg)
    payload = {
        "session_id": session_id,
        "hostname": str(ident.get("effective_hostname") or ""),
        "mcc_host_name": str(ident.get("effective_mcc_host_name") or ""),
        "agent_hostname": str(ident.get("local_hostname") or ""),
        "configured_host_name": str(ident.get("configured_host_name") or ""),
        "result_status": result_status,
        "result_message": result_message,
        "new_version": new_version,
    }
    url = base + "/api/v1/agent/update/release"
    try:
        _post_json(url, payload, cfg.mcc_token, timeout_sec=8)
    except Exception:
        return


def _download_package(url: str, dst: Path, token: str | None) -> None:
    req = request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=30) as resp:
        dst.write_bytes(resp.read())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _copytree_ignore_artifacts(_src_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lower = name.lower()
        if name.startswith("._") or name.startswith(".__"):
            ignored.add(name)
            continue
        if name in {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}:
            ignored.add(name)
            continue
        if lower in {".ds_store"}:
            ignored.add(name)
            continue
        if lower.startswith(".venv"):
            ignored.add(name)
            continue
    return ignored


def _extract_archive_to_dir(archive_path: Path, dst_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mcd-self-update-") as td:
        td_path = Path(td)
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(td_path)
        entries = [x for x in td_path.iterdir() if not x.name.startswith(".")]
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else td_path
        if not (root / "mcd_agent").exists():
            raise RuntimeError("update archive does not contain mcd_agent package")
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(
            root,
            dst_dir,
            symlinks=True,
            ignore=_copytree_ignore_artifacts,
        )


def _install_requirements_for_staged_source(install_dir: Path, staged_src_dir: Path) -> None:
    req = staged_src_dir / "requirements.txt"
    if not req.exists():
        logging.info("MCD self-update: requirements.txt not found in staged source, skip dependency sync")
        return
    venv_python = install_dir / "venv" / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"venv python not found: {venv_python}")
    cmd = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-r",
        str(req),
    ]
    proc = subprocess.run(cmd, cwd="/", capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "pip install failed").strip()
        raise RuntimeError(f"dependency install failed: {detail}")


def _pre_switch_smoke_check(install_dir: Path, staged_src_dir: Path) -> None:
    venv_python = install_dir / "venv" / "bin" / "python"
    if not venv_python.exists():
        raise RuntimeError(f"venv python not found: {venv_python}")

    compile_proc = subprocess.run(
        [str(venv_python), "-m", "compileall", "-q", str(staged_src_dir)],
        capture_output=True,
        text=True,
    )
    if compile_proc.returncode != 0:
        detail = (compile_proc.stderr or compile_proc.stdout or "compileall failed").strip()
        raise RuntimeError(f"pre-switch smoke failed (compileall): {detail}")

    smoke_script = f"""
import importlib
import pathlib
import sys

root = pathlib.Path({str(staged_src_dir)!r})
sys.path.insert(0, str(root))
for mod in (
    "mcd_agent",
    "mcd_agent.config",
    "mcd_agent.backup",
    "mcd_agent.self_update",
    "mcd_agent.daemon",
    "mcd_agent.cli",
):
    importlib.import_module(mod)

backup = importlib.import_module("mcd_agent.backup")
if not hasattr(backup, "backup_lock_active"):
    raise RuntimeError("backup_lock_active is missing in staged mcd_agent.backup")
"""
    smoke_proc = subprocess.run(
        [str(venv_python), "-c", smoke_script],
        capture_output=True,
        text=True,
    )
    if smoke_proc.returncode != 0:
        detail = (smoke_proc.stderr or smoke_proc.stdout or "import smoke failed").strip()
        raise RuntimeError(f"pre-switch smoke failed (import): {detail}")


def _restart_service_async() -> None:
    # Detached restart: current process can finish response and be replaced by systemd.
    subprocess.Popen(
        ["bash", "-lc", "(sleep 1; systemctl restart mcd) >/dev/null 2>&1 &"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _acquire_update_lock(cfg: AgentConfig, *, blocking: bool) -> Any | None:
    lock_p = _lock_path(cfg)
    lock_p.parent.mkdir(parents=True, exist_ok=True)
    lock_f = lock_p.open("w", encoding="utf-8")
    try:
        if fcntl is not None:
            op = fcntl.LOCK_EX
            if not blocking:
                op |= fcntl.LOCK_NB
            fcntl.flock(lock_f.fileno(), op)
        return lock_f
    except Exception:
        try:
            lock_f.close()
        except Exception:
            pass
        return None


def _release_update_lock(lock_f: Any | None) -> None:
    if lock_f is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_f.close()
    except Exception:
        pass


def _safe_mtime(path: Path, fallback: int) -> int:
    try:
        return int(path.stat().st_mtime)
    except Exception:
        return fallback


def _remove_path(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _prune_entries(paths: list[Path], *, keep_count: int, max_age_sec: int, now_s: int) -> int:
    keep = max(0, int(keep_count))
    age_limit = max(86_400, int(max_age_sec))
    ordered = sorted(paths, key=lambda p: _safe_mtime(p, now_s), reverse=True)
    removed = 0
    for idx, p in enumerate(ordered):
        age_sec = max(0, now_s - _safe_mtime(p, now_s))
        if idx < keep and age_sec <= age_limit:
            continue
        if _remove_path(p):
            removed += 1
    return removed


def _cleanup_update_artifacts(cfg: AgentConfig, *, now_s: int | None = None) -> dict[str, int]:
    now = int(time.time()) if now_s is None else int(now_s)
    install_dir = Path("/opt/mcd")
    updates_dir = install_dir / "var" / "updates"
    backup_dir = install_dir / "var" / "backup"

    keep_archives = max(0, int(cfg.mcd_update_keep_archives))
    keep_preupdate = max(0, int(cfg.mcd_update_keep_preupdate_backups))
    max_age_days = max(1, int(cfg.mcd_update_artifacts_max_age_days))
    max_age_sec = max_age_days * 86_400

    removed = {"archives": 0, "preupdate_backups": 0, "stale_dirs": 0}

    if updates_dir.exists():
        archives = [
            p
            for p in updates_dir.iterdir()
            if p.is_file()
            and p.name.startswith("mcd-agent-")
            and (p.name.endswith(".tar.gz") or p.name.endswith(".tgz") or p.name.endswith(".tar"))
        ]
        removed["archives"] = _prune_entries(archives, keep_count=keep_archives, max_age_sec=max_age_sec, now_s=now)

        stale_stage_dirs = [
            p
            for p in updates_dir.iterdir()
            if p.is_dir() and (p.name.startswith("src.next-") or p.name.startswith("src.prev-"))
        ]
        for p in stale_stage_dirs:
            # Keep very fresh staging dirs to avoid racing with non-MCD tooling.
            age_sec = max(0, now - _safe_mtime(p, now))
            if age_sec < 3600:
                continue
            if _remove_path(p):
                removed["stale_dirs"] += 1

    if backup_dir.exists():
        preupdate_backups = [
            p for p in backup_dir.iterdir() if p.is_file() and p.name.startswith("mcd-src-preupdate-") and p.name.endswith(".tgz")
        ]
        removed["preupdate_backups"] = _prune_entries(
            preupdate_backups,
            keep_count=keep_preupdate,
            max_age_sec=max_age_sec,
            now_s=now,
        )

    if any(v > 0 for v in removed.values()):
        logging.info(
            "MCD self-update cleanup: removed archives=%s preupdate_backups=%s stale_dirs=%s",
            removed["archives"],
            removed["preupdate_backups"],
            removed["stale_dirs"],
        )
    return removed


def _maybe_run_update_cleanup(cfg: AgentConfig, state: dict[str, Any], *, now_s: int) -> bool:
    if not bool(cfg.mcd_update_cleanup_enabled):
        return False
    interval_sec = max(300, int(cfg.mcd_update_cleanup_interval_sec or 86_400))
    next_cleanup_ts = int(state.get("next_cleanup_ts", 0) or 0)
    if now_s < next_cleanup_ts:
        return False

    lock_f = _acquire_update_lock(cfg, blocking=False)
    if lock_f is None:
        state["last_cleanup_status"] = "deferred_lock"
        state["next_cleanup_ts"] = now_s + min(300, interval_sec)
        return True
    try:
        removed = _cleanup_update_artifacts(cfg, now_s=now_s)
        state["last_cleanup_ts"] = now_s
        state["last_cleanup_status"] = "ok"
        state["last_cleanup_removed"] = removed
        state["next_cleanup_ts"] = now_s + interval_sec
        return True
    except Exception as e:
        state["last_cleanup_ts"] = now_s
        state["last_cleanup_status"] = f"failed:{e}"
        state["next_cleanup_ts"] = now_s + interval_sec
        logging.warning("MCD self-update cleanup failed: %s", e)
        return True
    finally:
        _release_update_lock(lock_f)


def apply_update(cfg: AgentConfig, plan: dict[str, Any]) -> tuple[bool, str]:
    status = str(plan.get("status", "")).strip().lower()
    if status != "update":
        return False, f"no update plan (status={status or '-'})"
    target = str(plan.get("target", "")).strip()
    package_url = str(plan.get("package_url", "")).strip()
    if not target or not package_url:
        return False, "invalid plan: target/package_url is required"
    if _semver(target) <= _semver(__version__):
        return False, f"already up-to-date ({__version__})"

    state = _read_state(cfg)
    session_id = str(plan.get("session_id", "")).strip()
    install_dir = Path("/opt/mcd")
    src_dir = install_dir / "src"
    backup_dir = install_dir / "var" / "backup"
    updates_dir = install_dir / "var" / "updates"
    src_next_dir = updates_dir / f"src.next-{target}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    updates_dir.mkdir(parents=True, exist_ok=True)

    lock_f = _acquire_update_lock(cfg, blocking=False)
    if lock_f is None:
        session_id = str(plan.get("session_id", "")).strip()
        if session_id:
            release_session(
                cfg,
                session_id,
                result_status="failed",
                result_message="another update is already running",
                new_version=__version__,
            )
        return False, "another update is already running"

    now_s = int(time.time())
    archive_name = f"mcd-agent-{target}.tar.gz"
    archive_path = updates_dir / archive_name
    backup_path = backup_dir / f"mcd-src-preupdate-{now_s}.tgz"
    expected_sha = str(plan.get("sha256", "")).strip().lower()

    old_src_dir: Path | None = None
    swapped = False
    try:
        _download_package(package_url, archive_path, cfg.mcc_token)
        if expected_sha:
            actual = _sha256_file(archive_path).lower()
            if actual != expected_sha:
                raise RuntimeError(f"sha256 mismatch expected={expected_sha} actual={actual}")

        if src_dir.exists():
            with tarfile.open(backup_path, "w:gz") as tf:
                tf.add(src_dir, arcname="src")

        if src_next_dir.exists():
            shutil.rmtree(src_next_dir)
        _extract_archive_to_dir(archive_path, src_next_dir)
        _install_requirements_for_staged_source(install_dir, src_next_dir)
        _pre_switch_smoke_check(install_dir, src_next_dir)

        old_src_dir = updates_dir / f"src.prev-{now_s}"
        if old_src_dir.exists():
            shutil.rmtree(old_src_dir)
        if src_dir.exists():
            os.replace(src_dir, old_src_dir)
        os.replace(src_next_dir, src_dir)
        swapped = True

        release_session(
            cfg,
            session_id,
            result_status="success",
            result_message=f"updated to {target}",
            new_version=target,
        )
        state.update(
            {
                "last_status": "success",
                "last_result": f"updated to {target}",
                "last_target": target,
                "last_attempt_ts": now_s,
                "last_session_id": session_id,
            }
        )
        if bool(cfg.mcd_update_cleanup_enabled):
            try:
                state["last_cleanup_removed"] = _cleanup_update_artifacts(cfg, now_s=now_s)
                state["last_cleanup_ts"] = now_s
                state["last_cleanup_status"] = "ok"
                state["next_cleanup_ts"] = now_s + max(300, int(cfg.mcd_update_cleanup_interval_sec or 86_400))
            except Exception as ce:
                state["last_cleanup_status"] = f"failed:{ce}"
        _write_state(cfg, state)
        try:
            if old_src_dir is not None and old_src_dir.exists():
                shutil.rmtree(old_src_dir)
        except Exception:
            pass
        _restart_service_async()
        return True, f"update applied -> {target}; source switched, service restart scheduled"
    except Exception as e:
        msg = str(e)
        # Best-effort rollback from pre-update source copy.
        try:
            if swapped and old_src_dir is not None and old_src_dir.exists():
                if src_dir.exists():
                    shutil.rmtree(src_dir)
                os.replace(old_src_dir, src_dir)
            elif backup_path.exists():
                if src_dir.exists():
                    shutil.rmtree(src_dir)
                with tarfile.open(backup_path, "r:gz") as tf:
                    tf.extractall(install_dir)
        except Exception:
            pass
        try:
            if src_next_dir.exists():
                shutil.rmtree(src_next_dir)
        except Exception:
            pass
        release_session(
            cfg,
            session_id,
            result_status="failed",
            result_message=msg,
            new_version=__version__,
        )
        state.update(
            {
                "last_status": "failed",
                "last_result": msg,
                "last_target": target,
                "last_attempt_ts": now_s,
                "last_session_id": session_id,
            }
        )
        if bool(cfg.mcd_update_cleanup_enabled):
            try:
                state["last_cleanup_removed"] = _cleanup_update_artifacts(cfg, now_s=now_s)
                state["last_cleanup_ts"] = now_s
                state["last_cleanup_status"] = "ok"
                state["next_cleanup_ts"] = now_s + max(300, int(cfg.mcd_update_cleanup_interval_sec or 86_400))
            except Exception as ce:
                state["last_cleanup_status"] = f"failed:{ce}"
        _write_state(cfg, state)
        return False, msg
    finally:
        _release_update_lock(lock_f)


def maybe_auto_update(cfg: AgentConfig, *, force: bool = False) -> tuple[str | None, int]:
    state = _read_state(cfg)
    now_s = int(time.time())
    cleanup_state_changed = _maybe_run_update_cleanup(cfg, state, now_s=now_s)
    next_allowed = int(state.get("next_check_ts", 0) or 0)
    if not force and now_s < next_allowed:
        if cleanup_state_changed:
            _write_state(cfg, state)
        return None, max(1, next_allowed - now_s)

    if cfg.backup_enabled:
        try:
            if backup_lock_active(cfg):
                retry_sec = max(5, int(cfg.poll_interval_sec or 10))
                state["last_check_ts"] = now_s
                state["last_check_status"] = "deferred_backup_lock"
                state["next_check_ts"] = now_s + retry_sec
                _write_state(cfg, state)
                return "MCD update deferred: backup lock is active", retry_sec
        except Exception as e:
            logging.warning("MCD update backup lock check failed: %s", e)

    auto = bool(cfg.mcd_auto_update_enabled) and _update_policy(cfg) != "off"
    decision = check_with_mcc(cfg, auto_update_enabled=auto)
    status = str(decision.get("status", "")).strip().lower()
    retry_after = int(decision.get("retry_after_sec", 0) or 0)
    if retry_after <= 0:
        retry_after = int(cfg.mcd_update_check_interval_sec or 3600)

    state["last_check_ts"] = now_s
    state["last_check_status"] = status or "unknown"
    state["last_decision"] = decision

    if status in {"up_to_date", "disabled"}:
        state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
        _write_state(cfg, state)
        return None, int(state["next_check_ts"]) - now_s

    if status == "wait":
        state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_wait_retry_sec or retry_after))
        _write_state(cfg, state)
        return "MCD update: MCC slots busy, waiting for next retry", int(state["next_check_ts"]) - now_s

    if status in {"update", "update_available"}:
        target = str(decision.get("target", "")).strip()
        if _semver(target) <= _semver(__version__):
            state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
            _write_state(cfg, state)
            return None, int(state["next_check_ts"]) - now_s
        if status == "update_available" and not auto:
            state["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
            _write_state(cfg, state)
            return f"MCD update available: current={__version__} target={target}", int(state["next_check_ts"]) - now_s
        ok, msg = apply_update(cfg, decision)
        # apply_update() persists result fields (last_status/last_result/...).
        # Re-read to avoid overwriting them with stale pre-apply state.
        state2 = _read_state(cfg)
        state2["last_check_ts"] = now_s
        state2["last_check_status"] = status or "unknown"
        state2["last_decision"] = decision
        state2["next_check_ts"] = now_s + max(60, int(cfg.mcd_update_check_interval_sec))
        _write_state(cfg, state2)
        return msg, int(state2["next_check_ts"]) - now_s

    # Transport or protocol errors: avoid noisy retries.
    err_reason = str(decision.get("reason", status or "unknown")).strip()
    logging.warning("MCD update check failed: %s", err_reason)
    state["next_check_ts"] = now_s + max(120, int(cfg.mcd_update_check_interval_sec))
    _write_state(cfg, state)
    return None, int(state["next_check_ts"]) - now_s


def update_status(cfg: AgentConfig) -> dict[str, Any]:
    out = _read_state(cfg)
    out.setdefault("current_version", __version__)
    out.setdefault("policy", _update_policy(cfg))
    out.setdefault("auto_update_enabled", bool(cfg.mcd_auto_update_enabled))
    return out
