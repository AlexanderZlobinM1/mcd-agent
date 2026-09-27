import json
import os
import time

import pytest

from mcd_agent.mautic_prepared_registry import PreparedRegistry, load_readonly, PreparedRegistryError


def test_readonly_metadata_lookup_no_db_or_sidecar_writes(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    stage = root / "mcd-target-stage-fixture"
    stage.mkdir()
    st = stage.stat()
    database = root / "prepared.sqlite3"
    registry = PreparedRegistry(str(database))
    now = int(time.time())
    receipt = dict(prepared_target_id="p" * 32, binding={"fixture": "bound"}, issued_at=now, expires_at=now + 180)
    local = dict(stage_directory=str(stage), directory_device=st.st_dev, directory_inode=st.st_ino)
    with registry._connect() as connection:
        connection.execute("INSERT INTO prepared_targets VALUES (?,?,?,'prepared',NULL)",
            ("p" * 32, json.dumps(receipt), json.dumps(local)))
    before = (database.read_bytes(), database.stat().st_mtime_ns, set(root.iterdir()))
    value = load_readonly(str(database), "p" * 32, expected_binding=receipt["binding"], now=now)
    assert value["receipt"] == receipt
    assert before == (database.read_bytes(), database.stat().st_mtime_ns, set(root.iterdir()))
    with pytest.raises(PreparedRegistryError):
        load_readonly(str(database), "other", expected_binding=receipt["binding"], now=now)
    (root / "prepared.sqlite3-wal").write_bytes(b"pending")
    with pytest.raises(PreparedRegistryError):
        load_readonly(str(database), "p" * 32, expected_binding=receipt["binding"], now=now)
