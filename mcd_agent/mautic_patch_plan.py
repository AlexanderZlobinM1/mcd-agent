"""Revision-pinned, fail-closed Mautic 7.2 host patch-plan adapter."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from mcd_agent.install_type import detect_install_type

PLAN_SCHEMA = "mcd-mautic-patch-plan-v1"
REGISTRY_REVISION = "8829d322409c66f8ec9e9abf57c9ac42a19022cc"
MINIMUM_AGENT_VERSION = "1.2.5"
PREFLIGHT_SCHEMA = "mcd-mautic-patch-preflight-v1"
ROLE = "M7-ROLE-PERMISSIONS-HYDRATED-ROW"
ASSET = "M7-ASSET-MAPPER-WEBROOT"
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ROLE_PATH = "app/migrations/Version20211209022550.php"
_ROLE_720_VULNERABLE_SHA256 = "f970321517fa32eed01a031f5110f397e441bb049965efdbbeece7750df4d33c"
_ROLE_720_FIXED_SHA256 = "b690b3cdd927a9b8257572cbb7bc42aba79f6c8b90d1ce39bac3154a928f2328"
_BUNDLE_PATH = "app/bundles/CoreBundle/MauticCoreBundle.php"
_ASSET_PATH = "app/bundles/CoreBundle/DependencyInjection/Compiler/AssetMapperWebRootPass.php"
_ROLE_OLD = """        foreach ($roles as $role) {
            $rawPermissions = $role->getRawPermissions();"""
_ROLE_NEW = """        foreach ($roles as $roleResult) {
            // RoleRepository adds a scalar user count to this query, so Doctrine
            // hydrates each row as [Role, user_count] instead of Role.
            $role = is_array($roleResult) ? ($roleResult[0] ?? null) : $roleResult;
            if (!$role instanceof Role) {
                continue;
            }

            $rawPermissions = $role->getRawPermissions();"""
_BUNDLE_OLD = "        $container->addCompilerPass(new Compiler\\SystemThemeTemplatePathPass(), PassConfig::TYPE_BEFORE_REMOVING, 0);"
_BUNDLE_NEW = _BUNDLE_OLD + "\n        $container->addCompilerPass(new Compiler\\AssetMapperWebRootPass(), PassConfig::TYPE_BEFORE_REMOVING, 0);"
_ASSET_SOURCE = """<?php

declare(strict_types=1);

namespace Mautic\\CoreBundle\\DependencyInjection\\Compiler;

use Mautic\\CoreBundle\\Loader\\ParameterLoader;
use Symfony\\Component\\Config\\Resource\\FileResource;
use Symfony\\Component\\DependencyInjection\\Compiler\\CompilerPassInterface;
use Symfony\\Component\\DependencyInjection\\ContainerBuilder;

final class AssetMapperWebRootPass implements CompilerPassInterface
{
    private const PUBLIC_ASSETS_PATH_RESOLVER_ID         = 'asset_mapper.public_assets_path_resolver';
    private const LOCAL_PUBLIC_ASSETS_FILESYSTEM_ID      = 'asset_mapper.local_public_assets_filesystem';
    private const COMPILED_ASSET_MAPPER_CONFIG_READER_ID = 'asset_mapper.compiled_asset_mapper_config_reader';
    private const DEFAULT_PUBLIC_PREFIX                  = '/assets/build/';

    public function process(ContainerBuilder $container): void
    {
        $webRoot = $this->resolveWebRoot($container);
        if (!$webRoot) {
            return;
        }

        if ($container->hasDefinition(self::LOCAL_PUBLIC_ASSETS_FILESYSTEM_ID)) {
            $container->findDefinition(self::LOCAL_PUBLIC_ASSETS_FILESYSTEM_ID)->replaceArgument(0, $webRoot);
        }

        if (!$container->hasDefinition(self::COMPILED_ASSET_MAPPER_CONFIG_READER_ID)) {
            return;
        }

        $publicPrefix = $this->resolvePublicPrefix($container);
        $container->findDefinition(self::COMPILED_ASSET_MAPPER_CONFIG_READER_ID)
            ->replaceArgument(0, $webRoot.'/'.ltrim($publicPrefix, '/'));
    }

    private function resolveWebRoot(ContainerBuilder $container): ?string
    {
        if ($container->hasParameter('mautic.local_root')) {
            $localRoot = $container->getParameter('mautic.local_root');
            if (is_string($localRoot) && '' !== trim($localRoot) && !str_contains($localRoot, '%env(')) {
                return rtrim($localRoot, '/');
            }
        }

        if (!$container->hasParameter('kernel.project_dir')) {
            return null;
        }

        $projectDir = $container->getParameter('kernel.project_dir');
        if (!is_string($projectDir) || '' === trim($projectDir)) {
            return null;
        }

        $projectDir   = rtrim($projectDir, '/');
        $composerFile = $projectDir.'/composer.json';
        if (is_file($composerFile)) {
            $container->addResource(new FileResource($composerFile));
        }

        return rtrim(ParameterLoader::getWebrootDir($projectDir), '/');
    }

    private function resolvePublicPrefix(ContainerBuilder $container): string
    {
        if (!$container->hasDefinition(self::PUBLIC_ASSETS_PATH_RESOLVER_ID)) {
            return self::DEFAULT_PUBLIC_PREFIX;
        }

        $publicPrefix = $container->findDefinition(self::PUBLIC_ASSETS_PATH_RESOLVER_ID)->getArgument(0);
        if (!is_string($publicPrefix) || '' === trim($publicPrefix)) {
            return self::DEFAULT_PUBLIC_PREFIX;
        }

        return $publicPrefix;
    }
}
"""
_PATCHES = {
    ROLE: {"phase_order": 10, "phases": ["post_source_install", "before_doctrine_migrations"], "depends_on": []},
    ASSET: {"phase_order": 20, "phases": ["post_source_install_before_asset_generation"], "depends_on": [ROLE]},
}


class PatchPlanError(RuntimeError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contract() -> dict[str, Any]:
    return {"schema": PLAN_SCHEMA, "registry_revision": REGISTRY_REVISION, "minimum_agent_version": MINIMUM_AGENT_VERSION,
            "source_version": "7.1.3", "target_version": "7.2.0", "install_types": ["zip", "composer"],
            "patches": [{"id": key, **value, "conflicts_with": []} for key, value in _PATCHES.items()]}


def parse_plan(raw: str) -> dict[str, Any]:
    try:
        plan = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PatchPlanError("invalid_plan_json") from exc
    expected = {"schema": PLAN_SCHEMA, "registry_revision": REGISTRY_REVISION, "source_version": "7.1.3", "target_version": "7.2.0"}
    if not isinstance(plan, dict) or any(plan.get(k) != v for k, v in expected.items()) or plan.get("install_type") not in {"zip", "composer"}:
        raise PatchPlanError("unknown_schema_or_registry_revision")
    patches = [{"id": key, **value, "conflicts_with": []} for key, value in _PATCHES.items()]
    if set(plan) != {"schema", "registry_revision", "source_version", "target_version", "install_type", "patches"} or plan["patches"] != patches:
        raise PatchPlanError("unknown_patch_id_or_plan_order")
    return plan


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if os.path.commonpath((str(root), str(path))) != str(root):
        raise PatchPlanError("root_containment_failed")
    return path


def _source_root(root: Path) -> Path:
    sources = []
    for relative in (".", "docroot", "public"):
        candidate = _inside(root, relative)
        if _inside(candidate, _ROLE_PATH).is_file():
            sources.append(candidate)
    if len(sources) != 1:
        raise PatchPlanError("ambiguous_or_missing_mautic_source_root")
    return sources[0]


def _target_version(root: Path) -> str | None:
    versions = set()
    for relative in ("composer.lock", "docroot/composer.lock", "public/composer.lock"):
        path = _inside(root, relative)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for package in data["packages"]:
                    if package.get("name") in {"mautic/core-lib", "mautic/core-bundle", "mautic/core"}:
                        versions.add(str(package.get("version", "")).removeprefix("v"))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise PatchPlanError("invalid_version_metadata") from exc
    for prefix in ("", "docroot/", "public/"):
        path = _inside(root, prefix + "app/bundles/CoreBundle/release_metadata.json")
        if path.is_file():
            try:
                versions.add(json.loads(path.read_text(encoding="utf-8"))["version"])
            except (ValueError, KeyError, TypeError) as exc:
                raise PatchPlanError("invalid_version_metadata") from exc
    return versions.pop() if len(versions) == 1 else None


def _gate(source: Path, ident: str) -> dict[str, Any]:
    if ident == ROLE:
        path = _inside(source, _ROLE_PATH)
        content = path.read_bytes()
        text = content.decode("utf-8")
        old, fixed, sha = text.count(_ROLE_OLD), text.count(_ROLE_NEW), _sha(content)
        state = "error"
        if (old, fixed) == (1, 0) and sha == _ROLE_720_VULNERABLE_SHA256:
            state = "vulnerable"
        elif (old, fixed) == (0, 1) and sha == _ROLE_720_FIXED_SHA256:
            state = "already"
        return {"id": ident, "state": state, "gate_logic": "exact_count_and_file_sha256", "files": [{
            "path": _ROLE_PATH, "sha256": sha, "vulnerable_count": old, "fixed_count": fixed,
            "expected_count": 1, "vulnerable_sha256": _ROLE_720_VULNERABLE_SHA256,
            "fixed_sha256": _ROLE_720_FIXED_SHA256,
        }]}
    asset, bundle = _inside(source, _ASSET_PATH), _inside(source, _BUNDLE_PATH)
    bundle_text = bundle.read_text(encoding="utf-8"); asset_sha = _sha(asset.read_bytes()) if asset.exists() else None
    registered = bundle_text.count("new Compiler\\AssetMapperWebRootPass()")
    state = "vulnerable" if not asset.exists() and bundle_text.count(_BUNDLE_OLD) == 1 and registered == 0 else "already" if asset_sha == _sha(_ASSET_SOURCE.encode()) and registered == 1 else "error"
    return {"id": ident, "state": state, "gate_logic": "asset_source_and_registration_exact", "files": [{"path": _ASSET_PATH, "sha256": asset_sha, "expected_sha256": _sha(_ASSET_SOURCE.encode())}, {"path": _BUNDLE_PATH, "sha256": _sha(bundle_text.encode()), "vulnerable_count": bundle_text.count(_BUNDLE_OLD), "fixed_count": registered}]}


def _save(run: Path, payload: dict[str, Any]) -> None:
    previous_path = _inside(run, "result.json")
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else {}
    history = previous.get("history", [])
    backups = {item["path"]: item for item in previous.get("backup_records", [])}
    for patch in payload.get("patches", []):
        for item in patch.get("backups", []):
            if item["path"] in backups and backups[item["path"]] != item:
                raise PatchPlanError("backup_evidence_changed")
            backups[item["path"]] = item
    stored = {**payload, "history": history + [payload], "backup_records": list(backups.values())}
    run.mkdir(parents=True, exist_ok=True); temp = run / "result.json.tmp"
    temp.write_text(json.dumps(stored, sort_keys=True, indent=2) + "\n", encoding="utf-8"); temp.replace(run / "result.json")


def _backup(run: Path, source: Path, relative: str) -> dict[str, Any]:
    path = _inside(source, relative); backup = _inside(run, "backups/" + relative); backup.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists(); before = path.read_bytes() if existed else b""
    if existed:
        with backup.open("xb") as stream:
            stream.write(before)
    return {"path": relative, "backup": str(backup), "existed": existed, "before_sha256": _sha(before) if existed else None}


def _lint(path: Path) -> None:
    result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode: raise PatchPlanError("php_syntax_check_failed")


def _apply(source: Path, run: Path, ident: str, gate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    for item in gate["files"]:
        candidate = _inside(source, item["path"])
        if (_sha(candidate.read_bytes()) if candidate.exists() else None) != item["sha256"]:
            raise PatchPlanError("source_changed_after_gate")
    paths = [_ROLE_PATH] if ident == ROLE else [_ASSET_PATH, _BUNDLE_PATH]
    changes = {}
    for relative in paths:
        path = _inside(source, relative)
        if relative == _ROLE_PATH:
            changes[relative] = path.read_bytes().replace(_ROLE_OLD.encode(), _ROLE_NEW.encode(), 1)
        elif relative == _ASSET_PATH:
            changes[relative] = _ASSET_SOURCE.encode()
        else:
            changes[relative] = path.read_bytes().replace(_BUNDLE_OLD.encode(), _BUNDLE_NEW.encode(), 1)
    if ident == ROLE and _sha(changes[_ROLE_PATH]) != _ROLE_720_FIXED_SHA256:
        raise PatchPlanError("unexpected_patch_result")
    backups = [_backup(run, source, item) for item in paths]
    for item in backups:
        item["after_sha256"] = _sha(changes[item["path"]])
    _save(run, {**context, "status": "pending", "patches": [{"id": ident, "state": "pending", "backups": backups}]})
    staged = []
    try:
        for relative, content in changes.items():
            path = _inside(source, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".mcd-patch-", suffix=".php", dir=path.parent)
            staged.append((path, Path(temporary)))
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            stat = path.stat() if path.exists() else path.parent.stat()
            os.chmod(temporary, (stat.st_mode & 0o777) if path.exists() else 0o644)
            if os.geteuid() == 0:
                os.chown(temporary, stat.st_uid, stat.st_gid)
            _lint(Path(temporary))
        for path, temporary in staged:
            original = next(item for item in backups if _inside(source, item["path"]) == path)
            if (_sha(path.read_bytes()) if path.exists() else None) != original["before_sha256"]:
                raise PatchPlanError("source_changed_after_gate")
            temporary.replace(path)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)
    return {"id": ident, "state": "applied", "backups": backups}


def rollback(root_value: str, raw_plan: str, run_id: str) -> dict[str, Any]:
    plan = parse_plan(raw_plan)
    if not _RUN.fullmatch(run_id): raise PatchPlanError("invalid_run_id")
    root = Path(root_value).resolve(strict=True); source = _source_root(root)
    result_path = _inside(root, ".mcd/patch-runs/" + run_id + "/result.json")
    if not result_path.is_file(): raise PatchPlanError("rollback_evidence_not_found")
    evidence = json.loads(result_path.read_text(encoding="utf-8"))
    if evidence.get("plan_sha256") != _sha(json.dumps(plan, sort_keys=True).encode()):
        raise PatchPlanError("stale_run_plan")
    restored: list[dict[str, Any]] = []
    records = list(reversed(evidence.get("backup_records", [])))
    for item in records:
        if item["path"] not in {_ROLE_PATH, _ASSET_PATH, _BUNDLE_PATH}:
            raise PatchPlanError("unknown_backup_path")
        path = _inside(source, item["path"])
        current = _sha(path.read_bytes()) if path.exists() else None
        if current not in {item.get("before_sha256"), item.get("after_sha256")}:
            return {"status": "error", "reason": "partial_application", "run_id": run_id, "rollback": [], "path": item["path"]}
        backup = _inside(result_path.parent, "backups/" + item["path"])
        if item.get("existed") and _sha(backup.read_bytes()) != item["before_sha256"]:
            raise PatchPlanError("backup_checksum_mismatch")
    for item in records:
        path = _inside(source, item["path"])
        backup = _inside(result_path.parent, "backups/" + item["path"])
        if item.get("existed"):
            path.write_bytes(backup.read_bytes())
        elif path.exists():
            path.unlink()
        restored.append({"path": item["path"], "state": "reverted", "backup": str(backup)})
    payload = {"status": "success", "operation": "rollback", "run_id": run_id, "plan_sha256": evidence["plan_sha256"], "rollback": restored}
    _save(result_path.parent, payload)
    return payload


def _preflight_snapshot(source: Path, run: Path, plan: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    snapshot_dir = _inside(run, "preflight-snapshot")
    for relative in (_ROLE_PATH, _BUNDLE_PATH, _ASSET_PATH):
        path = _inside(source, relative)
        existed = path.is_file()
        data = path.read_bytes() if existed else b""
        backup = _inside(snapshot_dir, relative)
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("xb") as stream:
                stream.write(data)
        records.append({"path": relative, "existed": existed, "sha256": _sha(data) if existed else None})
    snapshot_id = _sha(json.dumps({"plan": plan, "files": records}, sort_keys=True).encode())
    return {"snapshot_id": snapshot_id, "files": records}


def _preflight_restore(source: Path, run: Path, snapshot: dict[str, Any], allowed_after: dict[str, set[str]]) -> tuple[bool, list[dict[str, Any]], str | None]:
    records = snapshot.get("files") if isinstance(snapshot.get("files"), list) else []
    for item in records:
        relative = str(item.get("path", ""))
        if relative not in {_ROLE_PATH, _BUNDLE_PATH, _ASSET_PATH}:
            return False, [], "unknown_snapshot_path"
        path = _inside(source, relative)
        current = _sha(path.read_bytes()) if path.is_file() else None
        if current not in ({item.get("sha256")} | allowed_after.get(relative, set())):
            return False, [], "source_changed_during_patch_preflight"
    restored: list[dict[str, Any]] = []
    for item in records:
        relative = str(item["path"])
        path = _inside(source, relative)
        if item.get("existed"):
            backup = _inside(run, "preflight-snapshot/" + relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(backup.read_bytes())
        elif path.exists():
            path.unlink()
        restored.append({"path": relative, "sha256": item.get("sha256"), "state": "restored"})
    for item in records:
        path = _inside(source, str(item["path"]))
        if (_sha(path.read_bytes()) if path.is_file() else None) != item.get("sha256"):
            return False, restored, "restore_verification_failed"
    return True, restored, None


def atomic_preflight(root_value: str, raw_plan: str, run_id: str) -> dict[str, Any]:
    """Apply and verify the complete mandatory patch sequence atomically."""
    plan = parse_plan(raw_plan)
    if not _RUN.fullmatch(run_id):
        raise PatchPlanError("invalid_run_id")
    root = Path(root_value).resolve(strict=True)
    if detect_install_type(str(root)) != plan["install_type"]:
        raise PatchPlanError("install_type_mismatch")
    if _target_version(root) != plan["target_version"]:
        raise PatchPlanError("target_version_mismatch")
    source = _source_root(root)
    run = _inside(root, ".mcd/patch-runs/" + run_id)
    snapshot = _preflight_snapshot(source, run, plan)
    context: dict[str, Any] = {
        "schema": PREFLIGHT_SCHEMA, "operation": "patch_preflight", "run_id": run_id,
        "plan_sha256": _sha(json.dumps(plan, sort_keys=True).encode()),
        "snapshot_id": snapshot["snapshot_id"], "resolved_source_root": str(source),
        "upgrade_started": False, "selected": [ROLE, ASSET], "applied": [],
    }
    _save(run, {**context, "status": "pending", "snapshot": snapshot})
    evidence: list[dict[str, Any]] = []
    allowed_after = {
        _ROLE_PATH: {_ROLE_720_FIXED_SHA256},
        _BUNDLE_PATH: set(),
        _ASSET_PATH: set(),
    }
    try:
        for phase in ("post_source_install", "before_doctrine_migrations", "post_source_install_before_asset_generation"):
            result = execute(str(root), raw_plan, phase, run_id, "apply")
            evidence.append(result)
            if result.get("status") != "success":
                raise PatchPlanError(f"phase_failed:{phase}:{result.get('reason', 'unknown')}")
            for relative in allowed_after:
                path = _inside(source, relative)
                if path.is_file():
                    allowed_after[relative].add(_sha(path.read_bytes()))
            for item in result.get("patches", []):
                if item.get("id") not in context["applied"] and item.get("state") in {"applied", "already"}:
                    context["applied"].append(item["id"])
        verification = _verify_preflight(source)
        if verification["role"]["state"] != "already" or verification["asset"]["state"] != "already":
            raise PatchPlanError("patch_verification_failed")
        context.update({"status": "success", "verification": verification, "phases": evidence})
        _save(run, context)
        return context
    except Exception as exc:
        rollback_ok, restored, rollback_reason = _preflight_restore(source, run, snapshot, allowed_after)
        context.update({
            "status": "error", "reason": str(exc), "phases": evidence,
            "rollback_attempted": True, "rollback_succeeded": rollback_ok,
            "restored": restored, "pre_patch_hashes": {item["path"]: item["sha256"] for item in snapshot["files"]},
            "post_patch_hashes": {relative: (_sha(_inside(source, relative).read_bytes()) if _inside(source, relative).is_file() else None) for relative in (_ROLE_PATH, _BUNDLE_PATH, _ASSET_PATH)},
            "restore_hashes": {item["path"]: item.get("sha256") for item in restored},
            "hard_incident": not rollback_ok, "rollback_reason": rollback_reason,
        })
        if not rollback_ok:
            context["reason"] = "hard_incident:patch_preflight_rollback_failed"
        _save(run, context)
        return context


def _verify_preflight(source: Path) -> dict[str, Any]:
    return {"role": _gate(source, ROLE), "asset": _gate(source, ASSET)}


def execute(root_value: str, raw_plan: str, phase: str, run_id: str, operation: str = "apply") -> dict[str, Any]:
    plan = parse_plan(raw_plan)
    if not _RUN.fullmatch(run_id): raise PatchPlanError("invalid_run_id")
    root = Path(root_value).resolve(strict=True)
    local_type = detect_install_type(str(root))
    if local_type != plan["install_type"]: raise PatchPlanError("install_type_mismatch")
    if _target_version(root) != plan["target_version"]: raise PatchPlanError("target_version_mismatch")
    source = _source_root(root); selected = [item["id"] for item in plan["patches"] if phase in item["phases"]]
    if operation not in {"verify", "apply"} or not selected: raise PatchPlanError("unsupported_operation_or_phase")
    run = _inside(root, ".mcd/patch-runs/" + run_id)
    context = {"operation": operation, "run_id": run_id, "phase": phase, "registry_revision": REGISTRY_REVISION,
               "plan_sha256": _sha(json.dumps(plan, sort_keys=True).encode()), "resolved_source_root": str(source)}
    previous_path = _inside(run, "result.json")
    if previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous.get("plan_sha256") != context["plan_sha256"]:
            raise PatchPlanError("stale_run_plan")
        if operation == "apply" and (
            previous.get("status") != "success"
            or previous.get("operation") == "rollback"
        ) and not (
            previous.get("operation") == "patch_preflight"
            and previous.get("status") == "pending"
        ):
            raise PatchPlanError("incomplete_or_rolled_back_run")
    gates = [_gate(source, ident) for ident in selected]
    if any(item["state"] == "error" for item in gates):
        return {**context, "status": "error", "reason": "ambiguous_or_unknown_gate", "patches": gates}
    if operation == "verify": return {**context, "status": "success", "patches": gates}
    applied = []
    try:
        for gate in gates:
            if gate["id"] == ASSET and _gate(source, ROLE)["state"] != "already": raise PatchPlanError("dependency_unmet:" + ROLE)
            result = _apply(source, run, gate["id"], gate, context) if gate["state"] == "vulnerable" else {"id": gate["id"], "state": "already", "backups": []}
            result.update({"phase": phase, "gate": gate})
            applied.append(result)
    except Exception as exc:
        payload = {**context, "status": "error", "reason": str(exc), "gates": gates, "patches": applied, "rollback": "required"}; _save(run, payload); return payload
    payload = {**context, "status": "success", "patches": applied}; _save(run, payload); return payload
