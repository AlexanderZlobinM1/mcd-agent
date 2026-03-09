from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pymysql

from mcd_agent.models import DBConfig


class MauticDB:
    def __init__(self, cfg: DBConfig) -> None:
        self.cfg = cfg

    def _connect(self) -> pymysql.connections.Connection:
        # Bound DB network operations so one stuck query/socket cannot freeze
        # the scheduler loop for hours.
        return pymysql.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            password=self.cfg.password,
            database=self.cfg.name,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=120,
            write_timeout=30,
        )

    def _render_query(self, query_template: str, context: dict[str, str] | None = None) -> str:
        now_utc = datetime.now(timezone.utc)
        params: dict[str, str] = {
            "prefix": self.cfg.table_prefix,
            "now_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "window_start_utc_24h": (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if context:
            params.update(context)
        return query_template.format(**params)

    def fetch_ids(self, query_template: str, limit: int, context: dict[str, str] | None = None) -> list[int]:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            first = next(iter(row.values()), None)
            try:
                out.append(int(first))
            except (TypeError, ValueError):
                continue
            if len(out) >= limit:
                break
        return out

    def fetch_rows(
        self,
        query_template: str,
        limit: int = 5000,
        context: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            if len(out) >= limit:
                break
        return out

    def fetch_count(self, query_template: str, context: dict[str, str] | None = None) -> int:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row: Any = cur.fetchone()
        if not isinstance(row, dict):
            return 0
        value = next(iter(row.values()), 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def execute_sql_template(self, query_template: str, context: dict[str, str] | None = None) -> int:
        query = self._render_query(query_template, context=context)
        with self._connect() as conn:
            with conn.cursor() as cur:
                affected = cur.execute(query)
        try:
            return int(affected)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_ident(raw: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw or ""):
            raise ValueError(f"Unsafe SQL identifier: {raw!r}")
        return raw

    @staticmethod
    def _safe_table(raw: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw or ""):
            raise ValueError(f"Unsafe SQL table: {raw!r}")
        return raw

    def count_contacts_without_comm(
        self,
        *,
        email_field: str,
        phone_field: str,
        mode: str = "email_and_mobile",
    ) -> int:
        table = self._safe_table(f"{self.cfg.table_prefix}leads")
        email = self._safe_ident(email_field)
        if str(mode).strip().lower() == "email_only":
            where = f"(`{email}` IS NULL OR `{email}` = '')"
        else:
            phone = self._safe_ident(phone_field)
            where = (
                f"(`{email}` IS NULL OR `{email}` = '') "
                f"AND (`{phone}` IS NULL OR `{phone}` = '')"
            )
        query = f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE {where}"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row: Any = cur.fetchone()
        if not isinstance(row, dict):
            return 0
        value = next(iter(row.values()), 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def delete_contacts_without_comm(
        self,
        *,
        email_field: str,
        phone_field: str,
        mode: str = "email_and_mobile",
        max_delete: int,
    ) -> int:
        table = self._safe_table(f"{self.cfg.table_prefix}leads")
        email = self._safe_ident(email_field)
        if str(mode).strip().lower() == "email_only":
            where = f"(`{email}` IS NULL OR `{email}` = '')"
        else:
            phone = self._safe_ident(phone_field)
            where = (
                f"(`{email}` IS NULL OR `{email}` = '') "
                f"AND (`{phone}` IS NULL OR `{phone}` = '')"
            )
        limit = max(0, int(max_delete))
        query = f"DELETE FROM `{table}` WHERE {where}"
        if limit > 0:
            query += f" LIMIT {limit}"
        with self._connect() as conn:
            with conn.cursor() as cur:
                affected = cur.execute(query)
        try:
            return int(affected)
        except (TypeError, ValueError):
            return 0
