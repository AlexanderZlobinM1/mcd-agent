from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from mcd_agent.plugin_operations import (
    command_template,
    cron_matches,
    effective_values,
    legacy_cron_rules,
    operations_for_instance,
    schedule_due,
    scheduled_tasks,
)


@dataclass
class _Instance:
    root: str
    instance_uid: str = "instance-1"
    name: str = "instance"
    primary_domain: str = "example.test"
    domains: tuple[str, ...] = ("example.test",)


def _item(bundle: str) -> dict:
    return {
        "operation_key": "vendor:vendor_sync",
        "bundle": bundle,
        "legacy_values": {"interval_sec": 7200},
        "operation": {
            "id": "vendor_sync",
            "mode": "scheduled",
            "fields": [
                {"id": "enabled", "type": "boolean", "default": True},
                {"id": "interval_sec", "type": "integer", "default": 3600},
                {"id": "dry_run", "type": "boolean", "default": False},
            ],
            "schedule": {"type": "interval", "enabled_field": "enabled", "interval_field": "interval_sec"},
            "command": {
                "runner": "mautic_console",
                "name": "vendor:sync",
                "args": ["tenant", {"field": "dry_run", "flag": "--dry-run"}],
            },
            "legacy_cron": {
                "action": "comment",
                "match": ["bin/console", "vendor:sync"],
                "migrate_schedule": True,
                "interval_field": "interval_sec",
            },
        },
    }


def test_installed_bundle_is_required_and_generic_values_override_legacy(tmp_path: Path) -> None:
    root = tmp_path / "mautic"
    (root / "plugins" / "VendorBundle").mkdir(parents=True)
    inst = _Instance(str(root))
    item = _item("VendorBundle")
    config = SimpleNamespace(
        plugin_operations={inst.instance_uid: [item]},
        plugin_operation_instance_settings={
            inst.instance_uid: {item["operation_key"]: {"interval_sec": 1800, "dry_run": True}}
        },
    )

    installed = operations_for_instance(config, inst)
    assert installed == [item]
    values = effective_values(config, inst, installed[0])
    assert values == {"enabled": True, "interval_sec": 1800, "dry_run": True}
    assert command_template(installed[0], values) == "vendor:sync tenant --dry-run"
    assert schedule_due(
        installed[0], values, now_epoch=5000, now_local=datetime(2026, 1, 1, 3, 0), last_epoch=3000
    )


def test_legacy_cron_rule_is_derived_only_from_installed_catalog_operation(tmp_path: Path) -> None:
    root = tmp_path / "mautic"
    (root / "plugins" / "VendorBundle").mkdir(parents=True)
    inst = _Instance(str(root))
    item = _item("VendorBundle")
    config = SimpleNamespace(plugin_operations={inst.instance_uid: [item]})

    assert legacy_cron_rules(config, [inst]) == [
        {
            "root": str(root),
            "instance_key": "instance-1",
            "operation_key": "vendor:vendor_sync",
            "task_id": "vendor_sync",
            "action": "comment",
            "match": ["bin/console", "vendor:sync"],
            "migrate_schedule": True,
            "interval_field": "interval_sec",
            "enabled_field": "enabled",
        }
    ]


def test_missing_bundle_hides_operation(tmp_path: Path) -> None:
    inst = _Instance(str(tmp_path / "mautic"))
    item = _item("VendorBundle")
    config = SimpleNamespace(plugin_operations={inst.instance_uid: [item]})

    assert operations_for_instance(config, inst) == []


def test_effective_values_continue_past_unrelated_alias_block(tmp_path: Path) -> None:
    root = tmp_path / "mautic"
    (root / "plugins" / "VendorBundle").mkdir(parents=True)
    inst = _Instance(str(root))
    item = _item("VendorBundle")
    config = SimpleNamespace(
        plugin_operations={inst.instance_uid: [item]},
        plugin_operation_instance_settings={
            inst.instance_uid: {"another:operation": {"enabled": True}},
            str(root): {
                item["operation_key"]: {"enabled": True, "interval_sec": 7200}
            },
        },
    )

    values = effective_values(config, inst, item)

    assert values["enabled"] is True
    assert values["interval_sec"] == 7200


def test_local_legacy_runtime_is_resolved_from_catalog_mapping(tmp_path: Path) -> None:
    root = tmp_path / "mautic"
    (root / "plugins" / "VendorBundle").mkdir(parents=True)
    inst = _Instance(str(root))
    item = _item("VendorBundle")
    item["operation"]["legacy_runtime"] = {
        "instance_settings_key": "old_vendor_instance_settings",
        "field_map": {"enabled": "enabled", "interval_sec": "interval"},
        "global_fields": {"enabled": "old_vendor_enabled", "interval_sec": "old_vendor_interval"},
    }
    config = SimpleNamespace(
        plugin_operations={inst.instance_uid: [item]},
        plugin_operation_instance_settings={},
        plugin_operation_legacy_runtime={
            "old_vendor_enabled": True,
            "old_vendor_interval": 3600,
            "old_vendor_instance_settings": {
                inst.instance_uid: {"enabled": False, "interval": 7200}
            },
        },
    )

    installed = operations_for_instance(config, inst)

    assert installed[0]["legacy_values"] == {"enabled": False, "interval_sec": 7200}
    assert effective_values(config, inst, installed[0])["enabled"] is False

    item["operation"]["legacy_runtime"]["scalar_field"] = "interval_sec"
    item["legacy_values"] = {}
    config.plugin_operation_legacy_runtime["old_vendor_instance_settings"][inst.instance_uid] = 5400
    installed = operations_for_instance(config, inst)
    assert installed[0]["legacy_values"] == {"enabled": True, "interval_sec": 5400}


def test_cron_supports_ranges_steps_lists_and_standard_dom_dow_or() -> None:
    monday_morning = datetime(2026, 8, 17, 9, 15)

    assert cron_matches("*/15 8-10 * * 1-5", monday_morning)
    assert cron_matches("15 9 1 * 1", monday_morning)  # Monday matches even though it is not day 1.
    assert not cron_matches("10 9 * * 1-5", monday_morning)


def test_composite_tasks_support_fixed_intervals_and_conditional_joined_arguments(tmp_path: Path) -> None:
    root = tmp_path / "mautic"
    (root / "plugins" / "VendorBundle").mkdir(parents=True)
    inst = _Instance(str(root))
    item = {
        "operation_key": "vendor:composite",
        "bundle": "VendorBundle",
        "operation": {
            "id": "composite",
            "mode": "scheduled",
            "fields": [
                {"id": "enabled", "default": False},
                {"id": "override", "default": False},
                {"id": "run_time", "default": "03:00"},
            ],
            "tasks": [
                {
                    "id": "probe",
                    "schedule": {
                        "type": "interval",
                        "interval_field": "",
                        "interval_sec": 7,
                        "enabled_field": "enabled",
                    },
                    "command": {
                        "runner": "mautic_console",
                        "name": "vendor:sync",
                        "args": [
                            {"literal": "--probe"},
                            {
                                "field": "run_time",
                                "flag": "--time",
                                "flag_join": True,
                                "when": {"field": "override", "equals": True},
                            },
                        ],
                    },
                }
            ],
        },
    }
    config = SimpleNamespace(plugin_operations={inst.instance_uid: [item]})
    installed = operations_for_instance(config, inst)
    task = scheduled_tasks(installed[0])[0]
    values = {"enabled": True, "override": False, "run_time": "04:30"}
    assert command_template(installed[0], values, task=task) == "vendor:sync --probe"
    values["override"] = True
    assert command_template(installed[0], values, task=task) == "vendor:sync --probe --time=04:30"
    assert not schedule_due(
        installed[0], values, task=task, now_epoch=106, now_local=datetime(2026, 1, 1), last_epoch=100
    )
    assert schedule_due(
        installed[0], values, task=task, now_epoch=107, now_local=datetime(2026, 1, 1), last_epoch=100
    )
