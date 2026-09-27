"""Read and recheck prepared Composer bytes, without touching the live root."""
from __future__ import annotations

import hashlib
import os
import stat
import re
import time
import uuid
import pwd
from pathlib import Path

from mcd_agent.composer_target_manifest import ComposerManifestError, produce_manifest


def grant_cache_access(*, stage_directory: str, prepared_root: str, cache_root: str,
                       run_as_user: str) -> None:
    """Grant selected-group read access, never archive write permission."""
    directory, root, cache = map(Path, (stage_directory, prepared_root, cache_root))
    if (root.parent != directory or cache.parent != root or cache.name not in (".mcd-composer-cache", ".mcd-composer-home")
            or any(path.resolve() != path or path.is_symlink() for path in (directory, root, cache))):
        raise ComposerManifestError("invalid scoped cache access roots")
    user = pwd.getpwnam(run_as_user)
    paths = [cache]
    for parent, dirs, files in os.walk(cache, followlinks=False):
        paths.extend(Path(parent) / name for name in dirs + files)
    for path in paths:
        if path.is_symlink() or cache not in path.parents and path != cache:
            raise ComposerManifestError("cache symlink or path escape")
        st = path.stat()
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise ComposerManifestError("invalid cache entry")
    for path in paths:
        os.chown(path, os.geteuid(), user.pw_gid)
        os.chmod(path, 0o750 if path.is_dir() else 0o640)
    # Only the selected user's group may traverse; never expose stage secrets
    # to other users by making the copied application generally traversable.
    for path in (directory, root):
        os.chown(path, -1, user.pw_gid)
        os.chmod(path, (stat.S_IMODE(path.stat().st_mode) & 0o700) | 0o010)


def _read_regular(path: Path, *, maximum: int, retain: bool = False):
    if path.is_symlink() or path.resolve() != path:
        raise ComposerManifestError("prepared input must not use symlink aliases")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ComposerManifestError("invalid or oversized prepared input")
        digest = hashlib.sha256()
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 1048576)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ComposerManifestError("prepared input exceeded limit")
            digest.update(chunk)
            if retain:
                chunks.append(chunk)
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        current = os.stat(path, follow_symlinks=False)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise ComposerManifestError("prepared input changed during hashing")
        return (b"".join(chunks) if retain else digest.hexdigest())
    finally:
        os.close(fd)


def prepare_receipt(*, prepared_root: str, live_root: str, target_version: str,
                    archive_paths: dict[str, str], prepared_target_id: str,
                    binding: dict, issued_at: int, expires_at: int,
                    prepared_source_anchor=None, patch_plan=None) -> dict:
    """Snapshot an already resolved target; does not run Composer or download.

    Archive paths come only from the local preparation adapter, never MCC input.
    """
    root, live = Path(prepared_root), Path(live_root)
    if (not root.is_absolute() or not live.is_absolute() or root.resolve() != root
            or live.resolve() != live or root == live or root in live.parents or live in root.parents):
        raise ComposerManifestError("prepared and live roots must be canonical and disjoint")
    if (not isinstance(prepared_target_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", prepared_target_id)
            or not isinstance(archive_paths, dict)):
        raise ComposerManifestError("prepared identity and local archive map required")
    fields = {"job_id", "run_id", "patch_run_id", "patch_plan_sha256", "install_type",
              "policy_revision", "policy_sha256", "execution_context", "source_version", "target_version"}
    if isinstance(binding, dict) and "identity_association" in binding:
        fields.add("identity_association")
    if isinstance(binding, dict) and "root_mapping" in binding:
        fields.add("root_mapping")
    if not isinstance(binding, dict) or set(binding) != fields:
        raise ComposerManifestError("exact immutable prepared binding required")
    for key in ("job_id", "run_id", "patch_run_id"):
        if not isinstance(binding[key], str) or not binding[key].strip() or len(binding[key]) > 256:
            raise ComposerManifestError("invalid prepared run identity")
    for key in ("patch_plan_sha256", "policy_sha256"):
        if not isinstance(binding[key], str) or not re.fullmatch(r"[0-9a-f]{64}", binding[key]):
            raise ComposerManifestError("invalid prepared binding digest")
    if binding["install_type"] != "composer" or binding["target_version"] != target_version:
        raise ComposerManifestError("prepared applicability mismatch")
    if type(binding["policy_revision"]) is not int or binding["policy_revision"] < 0:
        raise ComposerManifestError("invalid prepared policy revision")
    context = binding["execution_context"]
    if (not isinstance(context, dict) or set(context) != {"host_id", "instance_uid", "application_root", "table_prefix"}
            or context["application_root"] != live_root or not isinstance(context["instance_uid"], str)
            or not context["instance_uid"]):
        raise ComposerManifestError("prepared execution context mismatch")
    try:
        if str(uuid.UUID(context["host_id"])) != context["host_id"]:
            raise ValueError("noncanonical host")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ComposerManifestError("invalid prepared host identity") from exc
    prefix = context["table_prefix"]
    if not isinstance(prefix, str) or (prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", prefix)):
        raise ComposerManifestError("invalid prepared table prefix")
    if "identity_association" in binding:
        from mcd_agent.mautic_identity_association import validate_association
        validate_association(binding["identity_association"], wire_context=context)
    if "root_mapping" in binding:
        mapping = binding["root_mapping"]
        if ("identity_association" not in binding or not isinstance(mapping, dict)
                or set(mapping) != {"schema", "project_root", "application_root_relative", "console_relative_path"}
                or mapping["schema"] != "mcd-mautic-root-mapping-v1"):
            raise ComposerManifestError("invalid prepared root mapping")
        from mcd_agent.mautic_root_mapping import discover_root_mapping
        if discover_root_mapping(live_root) != mapping:
            raise ComposerManifestError("prepared independent root mapping mismatch")
    versions = []
    for key in ("source_version", "target_version"):
        if not isinstance(binding[key], str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", binding[key]):
            raise ComposerManifestError("invalid prepared exact version")
        versions.append(tuple(map(int, binding[key].split("."))))
    if versions[1] <= versions[0]:
        raise ComposerManifestError("prepared transition must advance")
    if type(issued_at) is not int or type(expires_at) is not int or issued_at < 0 or not 0 < expires_at - issued_at <= 1800:
        raise ComposerManifestError("invalid prepared receipt lifetime")
    digests = {}
    for name, value in archive_paths.items():
        path = Path(value)
        if not path.is_absolute() or root not in path.parents:
            raise ComposerManifestError("archive outside prepared root")
        digests[name] = _read_regular(path, maximum=1073741824)
    composer_json = _read_regular(root / "composer.json", maximum=16777216, retain=True)
    composer_lock = _read_regular(root / "composer.lock", maximum=16777216, retain=True)
    produced = produce_manifest(target_version=target_version, composer_json=composer_json,
                                composer_lock=composer_lock, artifact_digests=digests,
                                prepared_source_anchor=prepared_source_anchor, patch_plan=patch_plan)
    if prepared_source_anchor is not None and prepared_source_anchor["patch_plan_sha256"] != binding["patch_plan_sha256"]:
        raise ComposerManifestError("prepared source anchor binding mismatch")
    return dict(schema="mcd-mautic-prepared-target-v2" if "root_mapping" in binding else "mcd-mautic-prepared-target-v1", prepared_target_id=prepared_target_id,
                target_version=target_version, binding=binding, issued_at=issued_at,
                expires_at=expires_at, target_artifact=produced["target_artifact"])


def recheck_receipt(receipt: dict, *, prepared_root: str, live_root: str,
                    archive_paths: dict[str, str], expected_binding: dict, now: int | None = None) -> dict:
    if not isinstance(receipt, dict) or set(receipt) != {"schema", "prepared_target_id", "target_version", "target_artifact", "binding", "issued_at", "expires_at"} or receipt["schema"] not in {"mcd-mautic-prepared-target-v1", "mcd-mautic-prepared-target-v2"}:
        raise ComposerManifestError("invalid prepared target receipt")
    if (receipt["schema"] == "mcd-mautic-prepared-target-v2") != ("root_mapping" in receipt["binding"]):
        raise ComposerManifestError("prepared receipt schema binding mismatch")
    current = int(time.time()) if now is None else now
    if type(current) is not int or type(receipt["issued_at"]) is not int or type(receipt["expires_at"]) is not int or not receipt["issued_at"] <= current < receipt["expires_at"]:
        raise ComposerManifestError("prepared receipt expired or not yet valid")
    if receipt["binding"] != expected_binding:
        raise ComposerManifestError("prepared binding drift")
    anchor = receipt["target_artifact"]["manifest"].get("prepared_source_anchor")
    if anchor is not None:
        from mcd_agent.mautic_prepared_source_anchor import validate_anchor
        from mcd_agent.mautic_patch_plan_v3 import _file
        from mcd_agent.mautic_patch_resolution import canonical_json_sha256
        validate_anchor(anchor)
        base = Path(prepared_root)
        application = base if anchor["application_root_relative"] == "." else _file(base, anchor["application_root_relative"])
        files = {}
        for name in anchor["files"]:
            path = _file(application, name)
            files[name] = _read_regular(path, maximum=16777216) if path.exists() else None
        fresh_anchor = dict(anchor, files=files, target_source_sha256=canonical_json_sha256(
            {"target_version": anchor["target_version"], "files": files}))
        if fresh_anchor != anchor:
            raise ComposerManifestError("prepared tracked source drift")
    fresh = prepare_receipt(prepared_root=prepared_root, live_root=live_root,
                            target_version=receipt["target_version"], archive_paths=archive_paths,
                            prepared_target_id=receipt["prepared_target_id"], binding=expected_binding,
                            issued_at=receipt["issued_at"], expires_at=receipt["expires_at"],
                            prepared_source_anchor=anchor)
    if fresh != receipt:
        raise ComposerManifestError("prepared target drift")
    return fresh
