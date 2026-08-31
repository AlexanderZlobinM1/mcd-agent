from __future__ import annotations

import os
from pathlib import Path

from mcd_agent.fs_permissions import effective_guard_identity, ensure_instance_permissions


def test_docker_guard_uses_descriptor_identity_not_host_php_user() -> None:
    assert effective_guard_identity(
        runtime="docker", runtime_user="10001:10001", host_user="www-data"
    ) == "10001:10001"
    assert effective_guard_identity(
        runtime="host", runtime_user="10001:10001", host_user="www-data"
    ) == "www-data"


def test_numeric_container_identity_is_accepted_without_host_user_lookup(tmp_path: Path) -> None:
    cache = tmp_path / "var" / "cache"
    cache.mkdir(parents=True)
    result = ensure_instance_permissions(
        root=str(tmp_path),
        run_as_user=f"{os.getuid()}:{os.getgid()}",
        guard_paths=["var/cache"],
        fix_console_exec=False,
    )
    assert result.checked_paths == ["var/cache"]
    assert result.repaired_paths == []
    assert result.errors == []
