"""Bounded strict JSON parsing for read-only verification inputs."""
import json


def strict_json(raw, *, max_bytes):
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise ValueError("verification_json_size_invalid")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("verification_json_duplicate_key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("verification_json_nonfinite")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
