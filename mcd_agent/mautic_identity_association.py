"""Explicit scoped/local identity binding; never infer or strip UID suffixes."""

import re
import uuid


def validate_association(value, *, wire_context, local_context=None):
    fields = {"schema", "host_id", "application_root", "table_prefix", "wire_instance_uid", "local_instance_uid"}
    if type(value) is not dict or set(value) != fields or value["schema"] != "mcd-instance-identity-association-v1":
        raise ValueError("scenario_identity_association_shape_invalid")
    if type(value["host_id"]) is not str or str(uuid.UUID(value["host_id"])) != value["host_id"]:
        raise ValueError("scenario_identity_association_host_invalid")
    for key in ("wire_instance_uid", "local_instance_uid"):
        uid = value[key]
        if type(uid) is not str or not uid or len(uid) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in uid):
            raise ValueError("scenario_identity_association_uid_invalid")
    root = value["application_root"]
    if type(root) is not str or not root.startswith("/") or root == "/" or "\x00" in root or any(p in ("", ".", "..") for p in root.split("/")[1:]):
        raise ValueError("scenario_identity_association_root_invalid")
    prefix = value["table_prefix"]
    if type(prefix) is not str or (prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", prefix)):
        raise ValueError("scenario_identity_association_prefix_invalid")
    if (any(value[key] != wire_context[key] for key in ("host_id", "application_root", "table_prefix"))
            or value["wire_instance_uid"] != wire_context["instance_uid"]):
        raise ValueError("scenario_identity_association_wire_mismatch")
    if local_context is not None:
        if type(local_context) is not dict or set(local_context) != {"local_instance_uid", "application_root", "table_prefix"}:
            raise ValueError("scenario_identity_association_local_context_invalid")
        if any(value[key] != local_context[key] for key in ("local_instance_uid", "application_root", "table_prefix")):
            raise ValueError("scenario_identity_association_local_mismatch")
    return value
