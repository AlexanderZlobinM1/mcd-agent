from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent.executor import build_mautic_exec_args
from mcd_agent.inventory import InstanceInventory
from mcd_agent.runtime_descriptor import discover_runtime_instances, load_runtime_descriptor


def _descriptor(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "instances" / "newlook"
    config = root / "config"
    config.mkdir(parents=True)
    local_php = config / "local.php"
    local_php.write_text(
        """<?php
$parameters = array(
  'db_host' => '172.30.0.1',
  'db_port' => '3306',
  'db_name' => 'baza_newlook',
  'db_user' => 'korisnik_newlook',
  'db_password' => 'secret',
  'db_table_prefix' => 'mt_',
  'site_url' => 'https://newlook.mauticrfp.sales-snap.com',
  'default_timezone' => 'Europe/Belgrade',
);
""",
        encoding="utf-8",
    )
    payload = {
        "schema": 1,
        "runtime": "docker",
        "instance_uid": "newlook.mauticrfp.sales-snap.com",
        "name": "newlook.mauticrfp.sales-snap.com",
        "primary_domain": "newlook.mauticrfp.sales-snap.com",
        "domains": ["newlook.mauticrfp.sales-snap.com"],
        "host_root": str(root),
        "runtime_root": "/opt/mautic",
        "console_path": "/opt/mautic/bin/console",
        "local_php_path": str(local_php),
        "container_name": "mauticrfp-newlook",
        "container_user": "10001:10001",
        "host_db_host": "127.0.0.1",
        "php_bin": "/usr/bin/php8.4",
        "image_ref": "default7",
        "mautic_major": 7,
    }
    descriptor_root = tmp_path / "descriptors"
    descriptor_root.mkdir()
    central = descriptor_root / "newlook.json"
    marker = root / ".mcd-runtime.json"
    text = json.dumps(payload)
    central.write_text(text, encoding="utf-8")
    marker.write_text(text, encoding="utf-8")
    central.chmod(0o640)
    marker.chmod(0o640)
    return central, marker


def test_runtime_descriptor_discovers_db_and_runtime(tmp_path: Path) -> None:
    central, _marker = _descriptor(tmp_path)
    descriptor = load_runtime_descriptor(central, require_root_owner=False)
    assert descriptor.container_name == "mauticrfp-newlook"
    assert descriptor.host_db_host == "127.0.0.1"
    installs = discover_runtime_instances(
        central.parent,
        supported_mautic_majors=[7],
        require_root_owner=False,
    )
    assert len(installs) == 1
    install = installs[0]
    assert install.runtime == "docker"
    assert install.runtime_id == "mauticrfp-newlook"
    assert install.runtime_image_ref == "default7"
    assert install.db is not None and install.db.name == "baza_newlook"
    assert install.db.host == "127.0.0.1"


def test_executor_routes_console_through_scoped_docker_exec(tmp_path: Path) -> None:
    central, _marker = _descriptor(tmp_path)
    descriptor = load_runtime_descriptor(central, require_root_owner=False)
    with patch("mcd_agent.executor.descriptor_for_root", return_value=descriptor):
        command = build_mautic_exec_args(
            php_bin="/usr/bin/php",
            root=str(descriptor.host_root),
            command="segments:update",
            instance_id=42,
            run_as_user="www-data",
        )
    assert command[:8] == [
        "/usr/bin/docker",
        "exec",
        "--user",
        "10001:10001",
        "--workdir",
        "/opt/mautic",
        "mauticrfp-newlook",
        "/usr/bin/php8.4",
    ]
    assert command[8:] == [
        "/opt/mautic/bin/console",
        "mautic:segments:update",
        "-i",
        "42",
        "--no-interaction",
    ]
    assert "sudo" not in command


def test_inventory_persists_runtime_metadata(tmp_path: Path) -> None:
    central, _marker = _descriptor(tmp_path)
    install = discover_runtime_instances(
        central.parent,
        supported_mautic_majors=[7],
        require_root_owner=False,
    )[0]
    inventory = InstanceInventory(str(tmp_path / "inventory.db"))
    inventory._upsert_install(install)
    inventory.conn.commit()
    restored = inventory.list_instances()[0]
    assert restored.runtime == "docker"
    assert restored.runtime_id == "mauticrfp-newlook"
    assert restored.runtime_root == "/opt/mautic"
    assert restored.runtime_image_ref == "default7"


def test_rescan_removes_instance_after_runtime_descriptor_is_deleted(tmp_path: Path) -> None:
    central, _marker = _descriptor(tmp_path)
    install = discover_runtime_instances(
        central.parent,
        supported_mautic_majors=[7],
        require_root_owner=False,
    )[0]
    inventory = InstanceInventory(str(tmp_path / "inventory.db"))
    inventory._upsert_install(install)
    inventory.conn.commit()
    assert inventory.count() == 1

    config = SimpleNamespace(
        discovery_roots=[],
        exclude_path_contains=[],
        supported_mautic_majors=[7],
        custom_instances=[],
    )
    with patch("mcd_agent.inventory.discover_mautic", return_value=[]):
        inventory.rescan(config)
    assert inventory.count() == 0
