from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from unittest.mock import patch

import pytest

from mcd_agent.mautic_external_backup import release_backup_satisfied
from mcd_agent.mautic_json_repair import issue_repair_authorization_context
from mcd_agent.mautic_upgrade_contract import JSON_REPAIR_PLAN_CONTRACT, KNOWN_JSON_COLUMNS


ROOT = "/var/www/fixture"
UID = "fixture"
SOURCE = "6.0.9"
TARGET = "7.1.3"


def _plan() -> dict[str, object]:
    return {
        "schema": JSON_REPAIR_PLAN_CONTRACT,
        "condition": "sqlstate_1253_json_collation_binary",
        "source_major": 6,
        "target_major": 7,
        "table_prefix": "ss_",
        "columns": [f"{row['table']}.{row['column']}" for row in KNOWN_JSON_COLUMNS],
        "action": "normalize_declared_json_columns",
    }


def _issued(tmp_path: Path, *, issued_at: int | None = None) -> tuple[str, Path, Path, Path]:
    now = int(time.time())
    completed = (issued_at if issued_at is not None else now) - 10
    marker = tmp_path / ".mcd-backup.json"
    marker.write_text(json.dumps({
        "status": "ok",
        "backup_id": "fixture:backup-1",
        "ts_utc": datetime.fromtimestamp(completed, timezone.utc).isoformat().replace("+00:00", "Z"),
        "dumped_instances": [{"instance_uid": UID, "root": ROOT}],
    }), encoding="utf-8")
    key = tmp_path / "signing.key"
    context = tmp_path / "context.json"
    plan_json = json.dumps(_plan(), sort_keys=True, separators=(",", ":"))
    if issued_at is None:
        issue_repair_authorization_context(
            root=ROOT, instance_uid=UID, source_version=SOURCE, target_version=TARGET,
            repair_plan_json=plan_json, backup_manifest_path=str(marker),
            key_path=str(key), output_path=str(context),
        )
    else:
        with patch("mcd_agent.mautic_json_repair.time.time", return_value=issued_at):
            issue_repair_authorization_context(
                root=ROOT, instance_uid=UID, source_version=SOURCE, target_version=TARGET,
                repair_plan_json=plan_json, backup_manifest_path=str(marker),
                key_path=str(key), output_path=str(context),
            )
    return plan_json, marker, key, context


def _check(plan_json: str | None, key: Path, context: Path, **changes: object) -> bool:
    inputs = dict(
        do_backup=False, requires_json_repair=True,
        repair_plan_json=plan_json, repair_auth_context_file=str(context),
        repair_auth_key_file=str(key), instance_uid=UID, root=ROOT,
        source_version=SOURCE, target_version=TARGET,
    )
    inputs.update(changes)
    return release_backup_satisfied(**inputs)


def test_signed_external_backup_satisfies_without_local_backup(tmp_path: Path) -> None:
    plan, marker, key, context = _issued(tmp_path)
    assert _check(plan, key, context)
    assert marker.is_file()


def test_standalone_or_missing_external_proof_still_requires_local_backup(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert not _check(None, missing, missing)
    assert not _check("{}", missing, missing, requires_json_repair=False)
    assert _check(None, missing, missing, do_backup=True)


@pytest.mark.parametrize("change", ["marker", "signature", "uid", "plan"])
def test_altered_external_proof_fails_closed(tmp_path: Path, change: str) -> None:
    plan, marker, key, context = _issued(tmp_path)
    if change == "marker":
        marker.write_bytes(marker.read_bytes() + b" ")
    elif change == "signature":
        payload = json.loads(context.read_text(encoding="utf-8"))
        payload["signature"] = "0" * 64
        context.write_text(json.dumps(payload), encoding="utf-8")
    elif change == "uid":
        with pytest.raises(ValueError):
            _check(plan, key, context, instance_uid="other")
        return
    else:
        plan = json.dumps({**_plan(), "table_prefix": "other_"})
    with pytest.raises(ValueError):
        _check(plan, key, context)


def test_expired_signed_backup_proof_fails_closed(tmp_path: Path) -> None:
    old = int(time.time()) - 1200
    plan, _marker, key, context = _issued(tmp_path, issued_at=old)
    with pytest.raises(ValueError):
        _check(plan, key, context)
