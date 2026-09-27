import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
import zipfile
import copy
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from mcd_agent.composer_prepared_target import prepare_receipt
from mcd_agent.mautic_patch_resolution import canonical_json_sha256
from mcd_agent.mautic_root_mapping import discover_root_mapping
from mcd_agent.mautic_target_patch_verification import collect_snapshot, validate_result, BINDINGS
from test_mautic_patch_plan_v3 import _plan
from mcd_agent.mautic_patch_plan_v3 import _validate_plan


def test_real_prepared_archive_catalog_and_drift(tmp_path, monkeypatch, capsys):
    base = tmp_path.resolve()
    live, prepared = base / "live", base / "mcd-target-stage-fixture/source"
    for root in (live, prepared):
        for name in ("app", "plugins", "bin"):
            (root / name).mkdir(parents=True)
        (root / "bin/console").write_bytes(b"fixture")
    (live / "app/release_metadata.json").write_text('{"version":"7.1.3"}')
    (prepared / "app/release_metadata.json").write_text('{"version":"7.2.0"}')
    for root, version in ((live, "7.1.3"), (prepared, "7.2.0")):
        (root / "app/bundles/CoreBundle").mkdir(parents=True)
        (root / "app/bundles/CoreBundle/release_metadata.json").write_text(json.dumps(dict(version=version)))
    original_plan = _plan()
    original_plan.update(trigger="upgrade_lifecycle", phase="dependency_update_preflight",
        source_version="7.1.3", target_version="7.2.0")
    original_plan["patches"][0].update(triggers=["upgrade_lifecycle"], phases=["before_cache_warmup"])
    original_hash = _validate_plan(original_plan)
    original_files = {}
    for name in original_plan["patches"][0]["source_paths"]:
        for root in (live, prepared):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"before\n")
        original_files[name] = hashlib.sha256(b"before\n").hexdigest()
    (prepared / "source.php").write_bytes(b"fixed")
    archive = prepared / "cache/package.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("package/source.php", "fixed")
    (prepared / "composer.json").write_bytes(b'{"require":{"fixture/package":"1.0.0"}}')
    (prepared / "composer.lock").write_text(json.dumps(dict(packages=[dict(name="fixture/package",
        version="1.0.0", dist=dict(type="zip"))])))
    for name in ("composer.json", "composer.lock"):
        (live / name).write_bytes((prepared / name).read_bytes())
    (live / "app/config").mkdir()
    (live / "app/config/local.php").write_text("<?php $parameters = ['db_host'=>'127.0.0.1', 'db_name'=>'fixture', 'db_user'=>'fixture', 'db_password'=>'fixture', 'db_table_prefix'=>'ss_'];")
    context = dict(host_id="00000000-0000-0000-0000-000000000001", instance_uid="wire-fixture",
        application_root=str(live), table_prefix="ss_")
    association = dict(schema="mcd-instance-identity-association-v1", host_id=context["host_id"],
        application_root=str(live), table_prefix="ss_", wire_instance_uid="wire-fixture", local_instance_uid="local-fixture")
    mapping = discover_root_mapping(str(live))
    binding = dict(job_id="job1", run_id="run1", patch_run_id=original_plan["run_id"], patch_plan_sha256=original_hash,
        install_type="composer", policy_revision=1, policy_sha256="b" * 64,
        execution_context=context, source_version="7.1.3", target_version="7.2.0",
        identity_association=association, root_mapping=mapping)
    anchor = dict(schema="mcd-mautic-prepared-source-anchor-v1", recipe="patch-source-paths-prepatch-v1",
        target_version="7.2.0", patch_plan_sha256=original_hash, application_root_relative=".", files=original_files,
        target_source_sha256=canonical_json_sha256(dict(target_version="7.2.0", files=original_files)))
    now = int(time.time())
    archives = {"fixture/package": str(archive)}
    receipt = prepare_receipt(prepared_root=str(prepared), live_root=str(live), target_version="7.2.0",
        archive_paths=archives, prepared_target_id="p" * 32, binding=binding,
        issued_at=now, expires_at=now + 180, prepared_source_anchor=anchor, patch_plan=original_plan)
    assert receipt["schema"] == "mcd-mautic-prepared-target-v2"
    record = dict(id="FIX-TEST", source_paths=["source.php"],
        gate_logic="vulnerable_all; fixed_all; mixed_or_unknown=error", gate=[
            dict(group="vulnerable", kind="exact_count", path="source.php", needle="old", expected_count=1),
            dict(group="fixed", kind="exact_count", path="source.php", needle="fixed", expected_count=1)])
    catalog = json.dumps([record], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    snapshot = dict(schema="mcc-mautic-target-patch-verification-plan-v1", job_id="job1", run_id="run1",
        prepared_target_id="p" * 32, original_patch_plan_sha256=original_hash,
        catalog_revision=hashlib.sha1(catalog).hexdigest(), catalog_sha256=hashlib.sha256(catalog).hexdigest(),
        target_artifact_sha256=canonical_json_sha256(receipt["target_artifact"]),
        execution_context=context, identity_association=association, root_mapping=mapping,
        excluded_records=[dict(id="FIX-TEST", record_sha256=canonical_json_sha256(record), record=record)])
    stage = SimpleNamespace(root=prepared, target_version="7.2.0", prepared_receipt=receipt,
        prepared_binding=binding, prepared_archive_paths=archives)
    expected = {key: snapshot[key] for key in BINDINGS}
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*") if p.is_file()}
    result = collect_snapshot(stage, snapshot, expected_binding=expected,
        catalog_bytes=catalog, original_patch_ids={row["id"] for row in original_plan["patches"]})
    assert result["status"] == "verified"
    assert validate_result(result, snapshot=snapshot) == result
    forged = copy.deepcopy(result)
    forged["records"][0]["gates"][1]["actual"] = True
    with pytest.raises(ValueError):
        validate_result(forged, snapshot=snapshot)
    from mcd_agent.mautic_prepared_registry import PreparedRegistry
    from mcd_agent.mautic_patch_context import load_readonly_discovery_config
    from mcd_agent.mautic_target_patch_collector import verify_registered_target
    from mcd_agent.mautic_target_verification_authorization import PURPOSE
    from mcd_agent.instance_uid import build_domain_uid
    uid = build_domain_uid(domain=None, root=str(live), name=live.name)
    association["local_instance_uid"] = uid
    # Receipt and snapshot share the independently established association.
    config_file = base / "config.toml"
    config_file.write_text("[discovery]\nroots = " + json.dumps([str(live)]) + "\n")
    config = load_readonly_discovery_config(str(config_file))
    config.mautic_scenario_registry_file = str(base / "registry.sqlite3")
    config.mcc_url, config.mcc_token = "https://mcc.example", "private-fixture-token"
    monkeypatch.setattr("mcd_agent.discovery._web_vhosts_from_server_configs", lambda: {})
    monkeypatch.setattr("mcd_agent.discovery.discover_runtime_instances", lambda **kw: [])
    metadata = dict(plan_sha256=original_hash, target_version="7.2.0", hashes=original_files,
        original_hashes=original_files, original_composer_hashes={name: hashlib.sha256((live / name).read_bytes()).hexdigest()
            for name in ("composer.json", "composer.lock")}, application_root_relative=".",
        target_source_sha256=anchor["target_source_sha256"],
        target_package_sha256=receipt["target_artifact"]["manifest"]["composer_lock_sha256"], facts_receipt=None)
    registry = PreparedRegistry(config.mautic_scenario_registry_file)
    registry.register(receipt, stage_directory=str(prepared.parent), prepared_root=str(prepared),
        archive_paths=archives, stage_metadata=metadata)
    from mcd_agent.mautic_prepared_restore import restore_stage
    restore_stage(registry.load("p" * 32, expected_binding=binding, now=now),
                  plan=original_plan, config=config, live_root=str(live))
    claims = {key: snapshot[key] for key in BINDINGS}
    claims.update(schema="mcc-mautic-target-patch-verification-authorization-v1", purpose=PURPOSE,
        verification_plan_sha256=canonical_json_sha256(snapshot), issued_at=now, expires_at=now + 180,
        nonce="a" * 32, signature="b" * 64)
    admitted = {key: snapshot[key] for key in BINDINGS}
    admitted.update(schema="mcc-mautic-target-patch-verification-admission-v1", status="accepted", purpose=PURPOSE,
        verification_plan_sha256=canonical_json_sha256(snapshot), authorization_context_sha256=canonical_json_sha256(claims),
        issued_at=now, expires_at=now + 180)
    response = MagicMock()
    response.__enter__.return_value = response
    response.status, response.read.return_value = 200, json.dumps(admitted).encode()
    opener = MagicMock()
    opener.open.return_value = response
    cli_arguments = None
    if os.geteuid() == 0:
        config_file.write_text("[discovery]\nroots = " + json.dumps([str(live)]) +
            "\n[mcc]\nurl = 'https://mcc.example'\ntoken = 'private-fixture-token'\n[runtime]\nmautic_scenario_registry_file = " +
            json.dumps(config.mautic_scenario_registry_file) + "\n")
        cli_arguments = ["mcd-cli", "mautic-target-patch-verify", "--config", str(config_file),
            "--root", str(live), "--prepared-target-id", "p" * 32, "--json"]
        for name, value in (("verification-plan", snapshot), ("authorization-context", claims), ("patch-plan", original_plan)):
            path = base / (name + ".json")
            path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
            path.chmod(0o600)
            cli_arguments += ["--" + name + "-file", str(path), "--" + name + "-sha256", canonical_json_sha256(value)]
        catalog_path = base / "effective-records.json"
        catalog_path.write_bytes(catalog)
        catalog_path.chmod(0o600)
        cli_arguments += ["--catalog-file", str(catalog_path), "--catalog-sha256", hashlib.sha256(catalog).hexdigest()]
    before_registered = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*") if p.is_file()}
    with patch("urllib.request.build_opener", return_value=opener):
        collected = verify_registered_target(config, root=str(live), prepared_target_id="p" * 32,
            snapshot=snapshot, catalog_bytes=catalog, original_plan=original_plan, authorization_context=claims)
    assert collected["status"] == "verified", collected
    if cli_arguments is not None:
        from mcd_agent import cli
        monkeypatch.setattr(sys, "argv", cli_arguments)
        with patch("urllib.request.build_opener", return_value=opener):
            assert cli.main() == 0
        assert json.loads(capsys.readouterr().out)["status"] == "verified"
    assert before_registered == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*") if p.is_file()}
    archive.write_bytes(b"replaced")
    with pytest.raises(ValueError):
        collect_snapshot(stage, snapshot, expected_binding=expected, catalog_bytes=catalog, original_patch_ids=set())
