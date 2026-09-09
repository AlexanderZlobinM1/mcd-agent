"""Revision-pinned, fail-closed Mautic 7.2 host patch-plan adapter."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

PLAN_SCHEMA = "mcd-mautic-patch-plan-v1"
REGISTRY_REVISION = "8829d322409c66f8ec9e9abf57c9ac42a19022cc"
MINIMUM_AGENT_VERSION = "1.2.2"
ROLE = "M7-ROLE-PERMISSIONS-HYDRATED-ROW"
ASSET = "M7-ASSET-MAPPER-WEBROOT"
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_ROLE_PATH = "app/migrations/Version20211209022550.php"
_BUNDLE_PATH = "app/bundles/CoreBundle/MauticCoreBundle.php"
_ASSET_PATH = "app/bundles/CoreBundle/DependencyInjection/Compiler/AssetMapperWebRootPass.php"
_ROLE_OLD = """foreach ($roles as $role) {
    $rawPermissions = $role->getRawPermissions();"""
_ROLE_NEW = """foreach ($roles as $roleResult) {
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
    sources = [item.resolve() for item in (root, root / "docroot", root / "public") if (item / _ROLE_PATH).is_file()]
    if len(sources) != 1:
        raise PatchPlanError("ambiguous_or_missing_mautic_source_root")
    return sources[0]


def _target_version(root: Path) -> str | None:
    pattern = re.compile(r"7\.2\.0")
    for relative in ("composer.lock", "app/config/local.php", "docroot/composer.lock", "docroot/app/config/local.php"):
        path = root / relative
        if path.is_file() and pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            return "7.2.0"
    return None


def _gate(source: Path, ident: str) -> dict[str, Any]:
    if ident == ROLE:
        path = _inside(source, _ROLE_PATH); text = path.read_text(encoding="utf-8")
        old, fixed = text.count(_ROLE_OLD), text.count("$role = is_array($roleResult)")
        state = "vulnerable" if (old, fixed) == (1, 0) else "already" if (old, fixed) == (0, 1) else "error"
        return {"id": ident, "state": state, "gate_logic": "exact_count_vulnerable_or_fixed", "files": [{"path": _ROLE_PATH, "sha256": _sha(text.encode()), "vulnerable_count": old, "fixed_count": fixed}]}
    asset, bundle = _inside(source, _ASSET_PATH), _inside(source, _BUNDLE_PATH)
    bundle_text = bundle.read_text(encoding="utf-8"); asset_sha = _sha(asset.read_bytes()) if asset.exists() else None
    registered = bundle_text.count("new Compiler\\AssetMapperWebRootPass()")
    state = "vulnerable" if not asset.exists() and bundle_text.count(_BUNDLE_OLD) == 1 and registered == 0 else "already" if asset_sha == _sha(_ASSET_SOURCE.encode()) and registered == 1 else "error"
    return {"id": ident, "state": state, "gate_logic": "asset_source_and_registration_exact", "files": [{"path": _ASSET_PATH, "sha256": asset_sha, "expected_sha256": _sha(_ASSET_SOURCE.encode())}, {"path": _BUNDLE_PATH, "sha256": _sha(bundle_text.encode()), "vulnerable_count": bundle_text.count(_BUNDLE_OLD), "fixed_count": registered}]}


def _save(run: Path, payload: dict[str, Any]) -> None:
    run.mkdir(parents=True, exist_ok=True); temp = run / "result.json.tmp"
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"); temp.replace(run / "result.json")


def _backup(run: Path, source: Path, relative: str) -> dict[str, Any]:
    path = _inside(source, relative); backup = run / "backups" / relative; backup.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists(); before = path.read_bytes() if existed else b""
    if existed: backup.write_bytes(before)
    return {"path": relative, "backup": str(backup), "existed": existed, "before_sha256": _sha(before) if existed else None}


def _lint(path: Path) -> None:
    result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode: raise PatchPlanError("php_syntax_check_failed")


def _apply(source: Path, run: Path, ident: str) -> dict[str, Any]:
    paths = [_ROLE_PATH] if ident == ROLE else [_ASSET_PATH, _BUNDLE_PATH]
    backups = [_backup(run, source, item) for item in paths]
    if ident == ROLE:
        path = _inside(source, _ROLE_PATH); path.write_text(path.read_text(encoding="utf-8").replace(_ROLE_OLD, _ROLE_NEW, 1), encoding="utf-8"); _lint(path)
    else:
        asset, bundle = _inside(source, _ASSET_PATH), _inside(source, _BUNDLE_PATH)
        asset.parent.mkdir(parents=True, exist_ok=True); asset.write_text(_ASSET_SOURCE, encoding="utf-8")
        bundle.write_text(bundle.read_text(encoding="utf-8").replace(_BUNDLE_OLD, _BUNDLE_NEW, 1), encoding="utf-8"); _lint(asset); _lint(bundle)
    for item in backups: item["after_sha256"] = _sha(_inside(source, item["path"]).read_bytes())
    return {"id": ident, "state": "applied", "backups": backups}


def rollback(root_value: str, raw_plan: str, run_id: str) -> dict[str, Any]:
    parse_plan(raw_plan)
    if not _RUN.fullmatch(run_id): raise PatchPlanError("invalid_run_id")
    root = Path(root_value).resolve(strict=True); source = _source_root(root)
    result_path = root / ".mcd" / "patch-runs" / run_id / "result.json"
    if not result_path.is_file(): raise PatchPlanError("rollback_evidence_not_found")
    evidence = json.loads(result_path.read_text(encoding="utf-8"))
    restored: list[dict[str, Any]] = []
    for patch in reversed(evidence.get("patches", [])):
        for item in reversed(patch.get("backups", [])):
            path = _inside(source, str(item["path"])); current = _sha(path.read_bytes()) if path.exists() else None
            if current != item.get("after_sha256"):
                return {"status": "error", "reason": "partial_application", "run_id": run_id, "rollback": restored, "path": item["path"]}
            if item.get("existed"):
                path.write_bytes(Path(item["backup"]).read_bytes())
            elif path.exists():
                path.unlink()
            restored.append({"path": item["path"], "state": "reverted", "backup": item["backup"]})
    payload = {"status": "success", "operation": "rollback", "run_id": run_id, "rollback": restored}
    _save(result_path.parent, payload)
    return payload


def execute(root_value: str, raw_plan: str, phase: str, run_id: str, operation: str = "apply") -> dict[str, Any]:
    plan = parse_plan(raw_plan)
    if not _RUN.fullmatch(run_id): raise PatchPlanError("invalid_run_id")
    root = Path(root_value).resolve(strict=True)
    local_type = "composer" if (root / "composer.json").is_file() else "zip"
    if local_type != plan["install_type"]: raise PatchPlanError("install_type_mismatch")
    if _target_version(root) != plan["target_version"]: raise PatchPlanError("target_version_mismatch")
    source = _source_root(root); selected = [item["id"] for item in plan["patches"] if phase in item["phases"]]
    if operation not in {"verify", "apply"} or not selected: raise PatchPlanError("unsupported_operation_or_phase")
    run = root / ".mcd" / "patch-runs" / run_id; gates = [_gate(source, ident) for ident in selected]
    if any(item["state"] == "error" for item in gates): return {"status": "error", "reason": "ambiguous_or_unknown_gate", "run_id": run_id, "phase": phase, "patches": gates}
    if operation == "verify": return {"status": "success", "operation": operation, "run_id": run_id, "phase": phase, "patches": gates}
    applied = []
    try:
        for gate in gates:
            if gate["id"] == ASSET and not (run / "result.json").is_file(): raise PatchPlanError("dependency_unmet:" + ROLE)
            applied.append(_apply(source, run, gate["id"]) if gate["state"] == "vulnerable" else {"id": gate["id"], "state": "already", "backups": []})
    except Exception as exc:
        payload = {"status": "error", "reason": str(exc), "run_id": run_id, "phase": phase, "patches": applied, "rollback": "required"}; _save(run, payload); return payload
    payload = {"status": "success", "operation": operation, "run_id": run_id, "phase": phase, "registry_revision": REGISTRY_REVISION, "plan_sha256": _sha(json.dumps(plan, sort_keys=True).encode()), "patches": applied}; _save(run, payload); return payload
