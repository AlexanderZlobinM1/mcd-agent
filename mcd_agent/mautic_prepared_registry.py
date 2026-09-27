"""Private durable prepared-target identity and once-only execution claims."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from pathlib import Path


class PreparedRegistryError(ValueError):
    pass


def load_readonly(path, ident, *, expected_binding, now):
    """Read committed metadata without creating SQLite files or sidecars."""
    from urllib.parse import quote
    database = Path(path)
    if not database.is_absolute() or database.resolve() != database or database.is_symlink():
        raise PreparedRegistryError("private canonical registry required")
    parent = database.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise PreparedRegistryError("private registry directory required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or before.st_mode & 0o077:
            raise PreparedRegistryError("private registry ownership required")
        wal = Path(str(database) + "-wal")
        if wal.exists():
            raise PreparedRegistryError("registry WAL requires reconciliation")
        uri = "file:" + quote(str(database), safe="/") + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            row = conn.execute("SELECT receipt,local_state,state,plan_sha256 FROM prepared_targets WHERE id=?", (ident,)).fetchone()
        after = os.stat(database, follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or wal.exists():
            raise PreparedRegistryError("registry changed during readonly lookup")
    finally:
        os.close(fd)
    if row is None:
        raise PreparedRegistryError("unknown prepared target")
    receipt, local = json.loads(row[0]), json.loads(row[1])
    if (receipt.get("prepared_target_id") != ident or (expected_binding is not None and receipt.get("binding") != expected_binding)
            or type(now) is not int or not receipt["issued_at"] <= now < receipt["expires_at"]):
        raise PreparedRegistryError("prepared identity binding or expiry mismatch")
    directory = Path(local["stage_directory"])
    if directory.is_symlink() or directory.resolve() != directory:
        raise PreparedRegistryError("prepared stage replaced")
    st = directory.stat()
    if (st.st_dev, st.st_ino, st.st_uid) != (local["directory_device"], local["directory_inode"], os.geteuid()):
        raise PreparedRegistryError("prepared stage identity mismatch")
    return dict(receipt=receipt, local=local, state=row[2], plan_sha256=row[3])


class PreparedRegistry:
    def __init__(self, path: str):
        parent = Path(path).parent
        if (not Path(path).is_absolute() or parent.resolve() != parent or parent.stat().st_uid != os.geteuid()
                or parent.stat().st_mode & 0o022):
            raise PreparedRegistryError("private canonical registry directory required")
        self.path = path
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise PreparedRegistryError("private registry ownership required")
        finally:
            os.close(fd)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS prepared_targets (id TEXT PRIMARY KEY,receipt TEXT NOT NULL,local_state TEXT NOT NULL,state TEXT NOT NULL,plan_sha256 TEXT)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def register(self, receipt: dict, *, stage_directory: str, prepared_root: str,
                 archive_paths: dict, stage_metadata: dict) -> None:
        directory, root = Path(stage_directory), Path(prepared_root)
        if (root.parent != directory or root.name != "source" or not directory.name.startswith("mcd-target-stage-")
                or directory.resolve() != directory or root.resolve() != root
                or directory.is_symlink() or root.is_symlink()):
            raise PreparedRegistryError("invalid local prepared stage")
        st = directory.stat()
        if st.st_uid != os.geteuid():
            raise PreparedRegistryError("prepared stage ownership mismatch")
        for path in archive_paths.values():
            candidate = Path(path)
            if root not in candidate.parents or candidate.resolve() != candidate:
                raise PreparedRegistryError("invalid local archive path")
        from mcd_agent.composer_prepared_target import recheck_receipt
        recheck_receipt(receipt, prepared_root=prepared_root,
                        live_root=receipt["binding"]["execution_context"]["application_root"],
                        archive_paths=archive_paths, expected_binding=receipt["binding"], now=receipt["issued_at"])
        local = dict(stage_directory=stage_directory, prepared_root=prepared_root,
                     archive_paths=archive_paths, metadata=stage_metadata,
                     directory_device=st.st_dev, directory_inode=st.st_ino)
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        local_json = json.dumps(local, sort_keys=True, separators=(",", ":"), allow_nan=False)
        ident = receipt.get("prepared_target_id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", ident):
            raise PreparedRegistryError("invalid prepared target ID")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT receipt,local_state FROM prepared_targets WHERE id=?", (ident,)).fetchone()
            if old:
                if old != (receipt_json, local_json):
                    raise PreparedRegistryError("prepared identity cannot be replaced")
                return
            conn.execute("INSERT INTO prepared_targets VALUES (?,?,?,'prepared',NULL)", (ident, receipt_json, local_json))

    def load(self, ident: str, *, expected_binding: dict, now: int) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT receipt,local_state,state,plan_sha256 FROM prepared_targets WHERE id=?", (ident,)).fetchone()
        if not row:
            raise PreparedRegistryError("unknown prepared target")
        receipt, local = json.loads(row[0]), json.loads(row[1])
        if receipt.get("binding") != expected_binding:
            raise PreparedRegistryError("prepared binding mismatch")
        if type(now) is not int or not receipt["issued_at"] <= now < receipt["expires_at"]:
            raise PreparedRegistryError("prepared receipt expired")
        directory = Path(local["stage_directory"])
        if directory.is_symlink() or directory.resolve() != directory:
            raise PreparedRegistryError("prepared stage replaced")
        st = directory.stat()
        if (st.st_dev, st.st_ino, st.st_uid) != (local["directory_device"], local["directory_inode"], os.geteuid()):
            raise PreparedRegistryError("prepared stage identity mismatch")
        return dict(receipt=receipt, local=local, state=row[2], plan_sha256=row[3])

    def bind_issued_plan(self, ident: str, plan_sha256: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
            raise PreparedRegistryError("invalid issued plan digest")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state,plan_sha256 FROM prepared_targets WHERE id=?", (ident,)).fetchone()
            if row == ("issued", plan_sha256):
                return
            if not row or row[0] != "prepared" or row[1] is not None:
                raise PreparedRegistryError("prepared target already issued or executing")
            conn.execute("UPDATE prepared_targets SET state='issued',plan_sha256=? WHERE id=?", (plan_sha256, ident))

    def claim_execution(self, ident: str, plan_sha256: str, *, expected_binding: dict, now: int) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT receipt FROM prepared_targets WHERE id=?", (ident,)).fetchone()
            if not row:
                raise PreparedRegistryError("unknown prepared target")
            receipt = json.loads(row[0])
            if (receipt["binding"] != expected_binding or type(now) is not int
                    or not receipt["issued_at"] <= now < receipt["expires_at"]):
                raise PreparedRegistryError("execution_binding_or_expiry_mismatch")
            count = conn.execute("UPDATE prepared_targets SET state='running' WHERE id=? AND state='issued' AND plan_sha256=?", (ident, plan_sha256)).rowcount
            if count != 1:
                raise PreparedRegistryError("execution_claim_denied_reconcile_required")

    def finish(self, ident: str, *, succeeded: bool) -> None:
        if type(succeeded) is not bool:
            raise PreparedRegistryError("invalid terminal outcome")
        with self._connect() as conn:
            changed = conn.execute("UPDATE prepared_targets SET state=? WHERE id=? AND state='running'",
                                   ("completed" if succeeded else "recovery_required", ident)).rowcount
            if changed != 1:
                raise PreparedRegistryError("execution not running")
