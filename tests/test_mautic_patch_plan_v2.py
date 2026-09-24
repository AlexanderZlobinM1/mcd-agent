import base64
import hashlib
import json

import pytest

from mcd_agent import mautic_patch_plan as v1
from mcd_agent import mautic_patch_plan_v2 as v2


def _payload(old="legacy", new="fixed"):
    return (
        "diff --git a/app/example.php b/app/example.php\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/example.php\n"
        "+++ b/app/example.php\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n+{new}\n"
    ).encode()


def _record(payload, *, absent_gate=False, phase="post_source_install", fixed_ranges=None):
    vulnerable_needle = "legacy" if not absent_gate else "migration_marker"
    vulnerable_count = 1 if not absent_gate else 0
    gates = [{"group": "vulnerable", "kind": "exact_count", "path": "app/example.php",
              "needle": vulnerable_needle, "expected_count": vulnerable_count}]
    if absent_gate:
        gates[0]["allow_missing_path"] = True
    gates.append({"group": "fixed", "kind": "exact_count", "path": "app/example.php",
                  "needle": "fixed", "expected_count": 1})
    return {
        "id": "M7-TEST-PATCH", "status": "active", "owner": "Mautic-Operations", "enabled": True,
        "execution_scope": "generic_upgrade", "affected_ranges": [">=7.2.0 while signature matches"],
        "fixed_ranges": fixed_ranges or [], "install_types": ["zip", "composer"],
        "phases": [phase], "depends_on": [], "conflicts_with": [], "phase_order": 10,
        "gate_logic": "vulnerable_all; fixed_all; mixed_or_unknown=error",
        "source_paths": ["app/example.php"], "patch_path": "patches/test.patch",
        "patch_sha256": hashlib.sha256(payload).hexdigest(), "gate": gates,
    }


def _plan(payload, record=None, **overrides):
    record = record or _record(payload)
    plan = {
        "schema": v2.SCHEMA, "registry_commit": "1" * 40, "registry_sha256": "2" * 64,
        "source_version": "7.1.3", "target_version": "7.2.1", "install_type": "zip",
        "phase": "post_source_install", "run_id": "test-run", "operation": "apply",
        "patches": [record],
        "payloads": [{"path": record["patch_path"], "sha256": record["patch_sha256"],
                       "content_base64": base64.b64encode(payload).decode()}],
    }
    plan.update(overrides)
    return json.dumps(plan, separators=(",", ":"))


def test_v2_contract_is_additive_and_advertises_literal_gate_capability():
    contract = v1.contract()
    assert contract["schema"] == v1.PLAN_SCHEMA
    assert v2.SCHEMA in contract["capabilities"]
    capability = contract["patch_plan_v2"]
    assert capability["gate_kinds"] == ["exact_count"]
    assert capability["gate_groups"] == ["vulnerable", "fixed"]
    assert capability["gate_semantics"] == "literal_substring_count"


def test_plan_accepts_future_same_major_target_without_registry_target_pin():
    raw = _plan(_payload(), target_version="7.2.3")
    parsed = v2.parse_plan(raw)
    assert parsed["target_version"] == "7.2.3"
    assert parsed["registry_commit"] == "1" * 40


@pytest.mark.parametrize("group,kind", [("fixed_cast", "exact_count"), ("vulnerable", "required_file"), ("asset", "exact_count")])
def test_v2_rejects_unsupported_gate_kinds_and_groups(group, kind):
    payload = _payload()
    record = _record(payload)
    record["gate"][0]["group"] = group
    record["gate"][0]["kind"] = kind
    with pytest.raises(v2.PatchPlanV2Error, match="unsupported_gate_kind_or_group"):
        v2.parse_plan(_plan(payload, record))


def test_v2_rejects_unflagged_missing_path_for_absence_gate(tmp_path):
    root = tmp_path / "mautic"
    root.mkdir()
    payload = _payload()
    record = _record(payload, absent_gate=True)
    record["gate"][0].pop("allow_missing_path")
    with pytest.raises(v2.PatchPlanV2Error, match="source_gate_path_missing"):
        v2._gate_results(root, record)


def test_v2_explicit_missing_zero_count_gate_is_a_candidate(tmp_path):
    root = tmp_path / "mautic"
    (root / "app").mkdir(parents=True)
    (root / "app/example.php").write_text("plain source\n")
    record = _record(_payload(), absent_gate=True)
    record["gate"][0]["path"] = "app/migrations/not-created-yet.php"
    assert v2._gate_results(root, record)[0] == "vulnerable"


def test_v2_fixed_signatures_win_when_vulnerable_absence_gate_still_matches(tmp_path):
    root = tmp_path / "mautic"
    source = root / "app"
    source.mkdir(parents=True)
    (source / "example.php").write_text("fixed\n")
    record = _record(_payload(), absent_gate=True)
    assert v2._gate_results(root, record)[0] == "fixed"


def test_v2_mixed_positive_vulnerable_and_fixed_signatures_fail_closed(tmp_path):
    root = tmp_path / "mautic"
    source = root / "app"
    source.mkdir(parents=True)
    (source / "example.php").write_text("legacy\nfixed\n")
    record = _record(_payload())
    assert v2._gate_results(root, record)[0] == "ambiguous_or_unknown"


def test_v2_applies_only_the_current_phase_and_rolls_back_from_signed_hashes(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    target = root / "app/example.php"
    target.parent.mkdir(parents=True)
    target.write_text("legacy\n")
    payload = _payload()
    record = _record(payload)
    record["phases"] = ["post_source_install", "before_doctrine_migrations"]
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    result = v2.execute(str(root), raw)
    assert result["schema"] == v2.EVIDENCE_SCHEMA
    assert result["status"] == "success"
    assert result["patches"][0]["decision"] == "applied"
    assert target.read_text() == "fixed\n"
    rolled_back = v2.rollback(str(root), raw)
    assert rolled_back["status"] == "success"
    assert target.read_text() == "legacy\n"


def test_v2_atomic_upgrade_preflight_advances_only_real_upgrade_phases(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    target = root / "app/example.php"
    target.parent.mkdir(parents=True)
    target.write_text("legacy\n")
    payload = _payload()
    record = _record(payload)
    record["phases"] = ["post_source_install", "before_doctrine_migrations", "before_background_imports"]
    raw = _plan(payload, record)
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    monkeypatch.setattr("mcd_agent.install_type.detect_install_type", lambda _root: "zip")
    result = v1.atomic_preflight(str(root), raw, "test-run")
    assert result["status"] == "success"
    assert [item["phase"] for item in result["phases"]] == [
        "post_source_install", "before_doctrine_migrations",
    ]
    assert result["patches"][0]["decision"] == "applied"
    assert target.read_text() == "fixed\n"
    later = json.loads(raw)
    later["phase"] = "before_background_imports"
    result = v1.execute(str(root), json.dumps(later), "before_background_imports", "test-run")
    assert result["status"] == "success"
    assert result["patches"][0]["decision"] == "already"


def test_v2_phase_with_no_matching_records_is_successful_noop(tmp_path, monkeypatch):
    root = tmp_path / "mautic"
    (root / "app").mkdir(parents=True)
    (root / "app/example.php").write_text("legacy\n")
    payload = _payload()
    record = _record(payload, phase="before_background_imports")
    raw = _plan(payload, record, phase="post_source_install")
    monkeypatch.setattr(v1, "_target_version", lambda _root: "7.2.1")
    result = v2.execute(str(root), raw)
    assert result["status"] == "success"
    assert result["selected"] == []
    assert result["patches"] == []
