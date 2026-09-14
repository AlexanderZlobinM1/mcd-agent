from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcd_agent import daemon
from mcd_agent.segment_recurring_priority import entries_for_instance, state_payload


def _inst() -> SimpleNamespace:
    return SimpleNamespace(
        instance_uid="electronic.sales-snap.com@MauticFarm-02",
        root="/var/www/electronic/public_html",
        name="electronic.sales-snap.com",
        primary_domain="electronic.sales-snap.com",
        domains=["electronic.sales-snap.com"],
    )


def test_entries_are_explicit_instance_scoped_deduplicated_and_validated() -> None:
    settings = {
        "default": {"segments": [{"id": 99, "max_interval_sec": 10}]},
        "electronic.sales-snap.com@MauticFarm-02": {
            "segments": [
                {"id": 86, "max_interval_sec": 60},
                {"id": "86", "max_interval_sec": 30},
                {"id": 87, "max_interval_sec": 9},
                {"id": 0, "max_interval_sec": 60},
            ]
        },
    }

    entries = entries_for_instance(settings, _inst())

    assert [(entry.segment_id, entry.max_interval_sec) for entry in entries] == [(86, 30)]
    assert entries[0].instance_uid == "electronic.sales-snap.com@MauticFarm-02"
    assert entries_for_instance({"default": settings["default"]}, _inst()) == []


def test_entries_match_canonical_uid_when_local_inventory_uid_is_legacy() -> None:
    inst = _inst()
    inst.instance_uid = "electronic.sales-snap.com"
    entries = entries_for_instance(
        {
            "electronic.sales-snap.com@MauticFarm-02": {
                "segments": [{"id": 86, "max_interval_sec": 60}],
            }
        },
        inst,
    )

    assert [(entry.segment_id, entry.max_interval_sec) for entry in entries] == [(86, 60)]
    assert entries[0].instance_uid == "electronic.sales-snap.com@MauticFarm-02"


def test_state_payload_exposes_complete_observation_contract() -> None:
    payload = state_payload(
        root="/var/www/electronic/public_html",
        instance_uid="electronic.sales-snap.com@MauticFarm-02",
        segment_id=86,
        max_interval_sec=60,
        active=False,
        pid=None,
        last_started_at=10.0,
        last_finished_at=20.0,
        last_status="failed",
        last_rc=1,
        last_error="exit_1",
        next_run_at=70.0,
        updated_at=20.0,
    )

    assert payload["schema"] == "mcd-segment-recurring-priority-v1"
    assert payload["last_error"] == "exit_1"
    assert payload["next_run_at"] == 70.0


def test_dispatch_uses_isolated_lane_and_publishes_completion() -> None:
    inst = _inst()
    entry = entries_for_instance(
        {inst.instance_uid: {"segments": [{"id": 86, "max_interval_sec": 60}]}},
        inst,
    )[0]
    store = SimpleNamespace(
        get_runtime_sync=Mock(return_value=None),
        put_runtime_sync=Mock(),
        list_runtime_sync=Mock(return_value=[]),
        delete_runtime_sync=Mock(),
    )
    executor = Mock()

    def launch(_config: object, **kwargs: object) -> bool:
        kwargs["on_start"](4321)
        kwargs["on_complete"](0)
        return True

    executor.launch.side_effect = launch
    executor.is_active.return_value = False
    cfg = SimpleNamespace(
        php_bin="php",
        mautic_run_as_user="www-data",
        cmd_segment_update_template="mautic:segments:update -i {id} --batch-limit={batch_limit}",
        segment_batch_limit=1000,
        command_timeout_sec=0,
        dispatch_interval_sec=1,
    )
    with patch.object(daemon, "render_mautic_command", return_value=["php", "bin/console", "mautic:segments:update", "-i", "86"]):
        launched = daemon._dispatch_recurring_priority_segments(
            config=cfg,
            inst=inst,
            store=store,
            running={},
            priority_executor=executor,
            entries=[entry],
            enabled=True,
        )

    assert launched == 1
    assert executor.launch.call_args.kwargs["capacity_lane"] == "segment_recurring_priority"
    assert executor.launch.call_args.kwargs["max_parallel"] == 1
    assert executor.launch.call_args.kwargs["interval_sec"] == 59
    assert store.put_runtime_sync.call_args_list[-1].args[1]["last_status"] == "ok"
    assert store.put_runtime_sync.call_args_list[-1].args[1]["instance_uid"] == inst.instance_uid


def test_dispatch_waits_for_existing_regular_segment_without_launching() -> None:
    inst = _inst()
    entry = entries_for_instance(
        {inst.instance_uid: {"segments": [{"id": 86, "max_interval_sec": 60}]}},
        inst,
    )[0]
    task = SimpleNamespace(root=inst.root, task_type="segment", entity_id=86)
    store = SimpleNamespace(
        get_runtime_sync=Mock(return_value={}),
        put_runtime_sync=Mock(),
        list_runtime_sync=Mock(return_value=[]),
        delete_runtime_sync=Mock(),
    )
    executor = Mock()

    launched = daemon._dispatch_recurring_priority_segments(
        config=SimpleNamespace(),
        inst=inst,
        store=store,
        running={"segment": task},
        priority_executor=executor,
        entries=[entry],
        enabled=True,
    )

    assert launched == 0
    executor.launch.assert_not_called()
    assert store.put_runtime_sync.call_args.args[1]["last_status"] == "waiting_overlap"


def test_disabled_or_removed_entries_are_pruned_without_launch() -> None:
    inst = _inst()
    stale_key = f"segment_recurring_priority:{inst.root}:86"
    store = SimpleNamespace(
        list_runtime_sync=Mock(return_value=[(stale_key, {})]),
        delete_runtime_sync=Mock(),
    )
    executor = Mock()

    launched = daemon._dispatch_recurring_priority_segments(
        config=SimpleNamespace(),
        inst=inst,
        store=store,
        running={},
        priority_executor=executor,
        entries=[],
        enabled=False,
    )

    assert launched == 0
    store.delete_runtime_sync.assert_called_once_with([stale_key])
    executor.launch.assert_not_called()
