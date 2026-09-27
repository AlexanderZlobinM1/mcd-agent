"""Byte-bound target manifest producer; staging/acquisition is a separate adapter."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA = "mcd-composer-target-manifest-v1"
SCHEMA_V2 = "mcd-composer-target-manifest-v2"


class ComposerManifestError(ValueError):
    pass


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ComposerManifestError("duplicate manifest input JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ComposerManifestError("non-finite manifest input")
    if not isinstance(data, bytes) or not data or len(data) > 16777216:
        raise ComposerManifestError("invalid or oversized Composer input")
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise ComposerManifestError("invalid Composer JSON") from exc
    if not isinstance(value, dict):
        raise ComposerManifestError("Composer JSON object required")
    return value


def canonical_manifest(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def validate_artifact(artifact: Any, *, target_version: str, patch_plan=None) -> dict:
    if (not isinstance(artifact, dict) or set(artifact) != {"kind", "sha256", "manifest"}
            or artifact["kind"] != "composer_manifest_sha256"):
        raise ComposerManifestError("invalid embedded Composer artifact")
    manifest = artifact["manifest"]
    fields = {"schema", "target_version", "include_dev", "composer_json_sha256", "composer_lock_sha256", "packages"}
    if isinstance(manifest, dict) and manifest.get("schema") == SCHEMA_V2:
        fields.add("prepared_source_anchor")
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest["schema"] not in (SCHEMA, SCHEMA_V2) or manifest["target_version"] != target_version
            or manifest["include_dev"] is not False):
        raise ComposerManifestError("invalid manifest fields or target binding")
    if manifest["schema"] == SCHEMA_V2:
        from mcd_agent.mautic_prepared_source_anchor import validate_anchor
        anchor = validate_anchor(manifest["prepared_source_anchor"], plan=patch_plan)
        if anchor["target_version"] != target_version:
            raise ComposerManifestError("prepared source anchor target mismatch")
    for key in ("composer_json_sha256", "composer_lock_sha256"):
        if not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[key]):
            raise ComposerManifestError("invalid Composer input digest")
    packages = manifest["packages"]
    if not isinstance(packages, list) or not 1 <= len(packages) <= 4096:
        raise ComposerManifestError("invalid manifest package list")
    names = []
    for package in packages:
        if not isinstance(package, dict) or set(package) != {"name", "version", "artifact_kind", "artifact_sha256"}:
            raise ComposerManifestError("invalid manifest package fields")
        name, version = package["name"], package["version"]
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name)
                or not isinstance(version, str) or not version or len(version) > 128):
            raise ComposerManifestError("invalid manifest package identity")
        names.append(name)
        digest = package["artifact_sha256"]
        if package["artifact_kind"] == "metapackage":
            if digest is not None:
                raise ComposerManifestError("metapackage digest must be null")
        elif package["artifact_kind"] == "dist_archive":
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ComposerManifestError("actual archive digest required")
        else:
            raise ComposerManifestError("unsupported package artifact kind")
    if names != sorted(set(names)):
        raise ComposerManifestError("package names must be sorted and unique")
    digest = artifact["sha256"]
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(canonical_manifest(manifest)).hexdigest() != digest):
        raise ComposerManifestError("embedded manifest digest mismatch")
    return json.loads(json.dumps(artifact, allow_nan=False))


def produce_manifest(*, target_version: str, composer_json: bytes,
                     composer_lock: bytes, artifact_digests: dict[str, str],
                     prepared_source_anchor=None, patch_plan=None) -> dict[str, Any]:
    """Consume verified staged artifact digests, never derive them from lock refs.

    This does not download packages or prove trusted approval of the target.
    Acquisition must independently hash actual archives and pin these inputs.
    Source-only installs are unsupported by this archive-only first schema.
    """
    if not isinstance(target_version, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", target_version):
        raise ComposerManifestError("invalid target version")
    _json(composer_json)
    lock = _json(composer_lock)
    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages or len(packages) > 4096:
        raise ComposerManifestError("resolved production package list required")
    if not isinstance(artifact_digests, dict):
        raise ComposerManifestError("verified artifact digest map required")
    records = []
    names = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ComposerManifestError("invalid resolved package")
        name, version = package.get("name"), package.get("version")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name)
                or name in names or not isinstance(version, str) or not version or len(version) > 128):
            raise ComposerManifestError("invalid or duplicate package identity")
        names.add(name)
        package_type = package.get("type", "library")
        if package_type == "metapackage":
            if name in artifact_digests:
                raise ComposerManifestError("metapackage cannot claim archive bytes")
            records.append(dict(name=name, version=version, artifact_kind="metapackage", artifact_sha256=None))
            continue
        if not isinstance(package.get("dist"), dict) or not package["dist"].get("type"):
            raise ComposerManifestError("source-only package is unsupported")
        digest = artifact_digests.get(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ComposerManifestError("actual package archive digest required")
        records.append(dict(name=name, version=version, artifact_kind="dist_archive", artifact_sha256=digest))
    archive_names = {row["name"] for row in records if row["artifact_kind"] == "dist_archive"}
    if set(artifact_digests) != archive_names:
        raise ComposerManifestError("artifact coverage mismatch")
    manifest = dict(schema=SCHEMA, target_version=target_version, include_dev=False,
                    composer_json_sha256=hashlib.sha256(composer_json).hexdigest(),
                    composer_lock_sha256=hashlib.sha256(composer_lock).hexdigest(),
                    packages=sorted(records, key=lambda row: row["name"]))
    if prepared_source_anchor is not None:
        from mcd_agent.mautic_prepared_source_anchor import validate_anchor
        manifest["schema"] = SCHEMA_V2
        manifest["prepared_source_anchor"] = validate_anchor(prepared_source_anchor, plan=patch_plan)
    encoded = canonical_manifest(manifest)
    artifact = dict(kind="composer_manifest_sha256", sha256=hashlib.sha256(encoded).hexdigest(), manifest=manifest)
    return dict(manifest=manifest, target_artifact=validate_artifact(artifact, target_version=target_version, patch_plan=patch_plan))
