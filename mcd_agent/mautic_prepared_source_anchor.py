"""Bounded pre-patch tracked-path anchor; never a whole installed-tree hash."""

import hashlib
import json
import re

from mcd_agent.mautic_patch_resolution import canonical_json_sha256

SCHEMA = "mcd-mautic-prepared-source-anchor-v1"
RECIPE = "patch-source-paths-prepatch-v1"


def _path(value):
    if type(value) is not str or len(value.encode("utf-8")) > 512:
        raise ValueError("prepared_anchor_path_invalid")
    parts = value.split("/")
    if (not value or value.startswith("/") or "\\" in value or ":" in parts[0]
            or any(part in {"", ".", ".."} for part in parts)):
        raise ValueError("prepared_anchor_path_unsafe")
    return value


def validate_anchor(anchor, *, plan=None):
    """Caller must independently validate the original immutable v3 plan."""
    fields = {"schema", "recipe", "target_version", "patch_plan_sha256",
              "application_root_relative", "files", "target_source_sha256"}
    if type(anchor) is not dict or set(anchor) != fields:
        raise ValueError("prepared_anchor_shape_invalid")
    if len(json.dumps(anchor, ensure_ascii=True, allow_nan=False).encode()) > 256 * 1024:
        raise ValueError("prepared_anchor_oversize")
    if anchor["schema"] != SCHEMA or anchor["recipe"] != RECIPE:
        raise ValueError("prepared_anchor_schema_invalid")
    if type(anchor["target_version"]) is not str or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", anchor["target_version"]):
        raise ValueError("prepared_anchor_version_invalid")
    if type(anchor["patch_plan_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", anchor["patch_plan_sha256"]):
        raise ValueError("prepared_anchor_plan_digest_invalid")
    if plan is not None and (anchor["target_version"] != plan["target_version"] or anchor["patch_plan_sha256"] != canonical_json_sha256(plan)):
        raise ValueError("prepared_anchor_plan_mismatch")
    if type(anchor["application_root_relative"]) is not str or anchor["application_root_relative"] not in {".", "docroot", "public"}:
        raise ValueError("prepared_anchor_layout_invalid")
    paths = {_path(path) for record in plan["patches"] for path in record["source_paths"]} if plan is not None else None
    files = anchor["files"]
    if type(files) is not dict or len(files) > 512 or (paths is not None and set(files) != paths):
        raise ValueError("prepared_anchor_path_set_mismatch")
    for path, digest in files.items():
        _path(path)
        if digest is not None and (type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("prepared_anchor_file_digest_invalid")
    expected = canonical_json_sha256({"target_version": anchor["target_version"], "files": files})
    if anchor["target_source_sha256"] != expected:
        raise ValueError("prepared_anchor_digest_mismatch")
    return anchor


def produce_anchor(stage, *, plan):
    from mcd_agent.mautic_patch_plan_v3 import _file, _read, _validate_plan
    plan_sha = _validate_plan(plan)
    if stage.plan_sha256 != plan_sha or stage.target_version != plan["target_version"]:
        raise ValueError("prepared_anchor_stage_binding_mismatch")
    application = stage.root_binding(stage.root)
    files = {}
    for record in plan["patches"]:
        for path in record["source_paths"]:
            _path(path)
            if path not in files:
                raw, _ = _read(_file(application, path))
                files[path] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if files != stage.hashes:
        raise ValueError("prepared_anchor_stage_bytes_changed")
    anchor = dict(schema=SCHEMA, recipe=RECIPE, target_version=stage.target_version,
                  patch_plan_sha256=plan_sha,
                  application_root_relative=application.relative_to(stage.root).as_posix(),
                  files=files, target_source_sha256=canonical_json_sha256(
                      {"target_version": stage.target_version, "files": files}))
    if anchor["target_source_sha256"] != stage.target_source_sha256:
        raise ValueError("prepared_anchor_stage_digest_changed")
    return validate_anchor(anchor, plan=plan)
