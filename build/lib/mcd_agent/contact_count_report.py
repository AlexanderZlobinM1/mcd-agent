from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcd_agent.db import MauticDB


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def collect_contact_count_report(db: MauticDB) -> dict[str, Any]:
    prefix = str(db.cfg.table_prefix or "")
    leads = db._safe_table(f"{prefix}leads")
    rows = db.fetch_rows(
        f"""
        SELECT
          COUNT(*) AS total_contacts,
          SUM(
            TRIM(COALESCE(`email`, '')) <> ''
            OR TRIM(COALESCE(`mobile`, '')) <> ''
          ) AS real_contacts,
          SUM(
            TRIM(COALESCE(`email`, '')) <> ''
            AND TRIM(COALESCE(`mobile`, '')) = ''
          ) AS email_only,
          SUM(
            TRIM(COALESCE(`email`, '')) = ''
            AND TRIM(COALESCE(`mobile`, '')) <> ''
          ) AS mobile_only,
          SUM(
            TRIM(COALESCE(`email`, '')) <> ''
            AND TRIM(COALESCE(`mobile`, '')) <> ''
          ) AS email_and_mobile,
          SUM(
            TRIM(COALESCE(`email`, '')) = ''
            AND TRIM(COALESCE(`mobile`, '')) = ''
          ) AS excluded_without_email_or_mobile
        FROM `{leads}`
        """,
        limit=1,
    )
    row = rows[0] if rows else {}
    payload = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_contacts": _count(row.get("total_contacts")),
        "real_contacts": _count(row.get("real_contacts")),
        "email_only": _count(row.get("email_only")),
        "mobile_only": _count(row.get("mobile_only")),
        "email_and_mobile": _count(row.get("email_and_mobile")),
        "excluded_without_email_or_mobile": _count(row.get("excluded_without_email_or_mobile")),
    }
    counted_parts = payload["email_only"] + payload["mobile_only"] + payload["email_and_mobile"]
    if counted_parts != payload["real_contacts"]:
        raise RuntimeError(
            "contact count consistency check failed: "
            f"real={payload['real_contacts']} parts={counted_parts}"
        )
    if payload["real_contacts"] + payload["excluded_without_email_or_mobile"] != payload["total_contacts"]:
        raise RuntimeError(
            "contact count total check failed: "
            f"total={payload['total_contacts']} classified="
            f"{payload['real_contacts'] + payload['excluded_without_email_or_mobile']}"
        )
    return payload
