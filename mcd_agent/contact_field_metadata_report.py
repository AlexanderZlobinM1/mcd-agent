from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcd_agent import __version__
from mcd_agent.db import MauticDB


SCHEMA = "mcd-contact-field-metadata-v1"
CAPABILITY = SCHEMA


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def contact_field_metadata_error(message: str, *, code: str = "collection_failed") -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": "1",
        "capability": CAPABILITY,
        "mcd_version": __version__,
        "status": "error",
        "generated_at": _generated_at(),
        "field_count": 0,
        "fields": [],
        "errors": [
            {
                "code": str(code or "collection_failed"),
                "message": str(message or "contact field metadata collection failed"),
                "retryable": True,
            }
        ],
    }


def collect_contact_field_metadata_report(db: MauticDB) -> dict[str, Any]:
    prefix = str(db.cfg.table_prefix or "")
    field_table = db._safe_table(f"{prefix}lead_fields")
    contact_table = db._safe_table(f"{prefix}leads")
    rows = db.fetch_rows(
        f"""
        SELECT
          lf.`alias` AS field_alias,
          lf.`label` AS field_label,
          CASE WHEN COALESCE(lf.`fixed`, 0) = 1 THEN 'native' ELSE 'custom' END AS field_classification,
          lf.`type` AS field_type,
          NULLIF(TRIM(COALESCE(lf.`group`, '')), '') AS field_group,
          NULLIF(TRIM(COALESCE(lf.`object`, '')), '') AS field_object,
          c.`DATA_TYPE` AS storage_type,
          c.`CHARACTER_MAXIMUM_LENGTH` AS max_length,
          c.`NUMERIC_PRECISION` AS numeric_precision,
          c.`NUMERIC_SCALE` AS numeric_scale
        FROM `{field_table}` lf
        LEFT JOIN `INFORMATION_SCHEMA`.`COLUMNS` c
          ON c.`TABLE_SCHEMA` = DATABASE()
         AND c.`TABLE_NAME` = '{contact_table}'
         AND c.`COLUMN_NAME` = lf.`alias`
        ORDER BY lf.`ordering`, lf.`id`
        """,
        limit=5000,
    )
    fields: list[dict[str, Any]] = []
    for row in rows:
        alias = str(row.get("field_alias") or "").strip()
        if not alias:
            continue
        classification = str(row.get("field_classification") or "custom").strip().lower()
        if classification not in {"native", "custom"}:
            classification = "custom"
        fields.append(
            {
                "alias": alias,
                "label": str(row.get("field_label") or ""),
                "type": str(row.get("field_type") or ""),
                "custom": classification == "custom",
                "classification": classification,
                "field_type": str(row.get("field_type") or ""),
                "group": str(row.get("field_group")) if row.get("field_group") is not None else None,
                "object": str(row.get("field_object")) if row.get("field_object") is not None else None,
                "storage_type": str(row.get("storage_type")) if row.get("storage_type") is not None else None,
                "max_length": _optional_int(row.get("max_length")),
                "numeric_precision": _optional_int(row.get("numeric_precision")),
                "numeric_scale": _optional_int(row.get("numeric_scale")),
            }
        )
    return {
        "schema": SCHEMA,
        "schema_version": "1",
        "capability": CAPABILITY,
        "mcd_version": __version__,
        "status": "ok",
        "generated_at": _generated_at(),
        "field_count": len(fields),
        "fields": fields,
        "errors": [],
    }
