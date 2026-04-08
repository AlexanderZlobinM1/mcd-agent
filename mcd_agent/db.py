from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pymysql

from mcd_agent.models import DBConfig


class MauticDB:
    def __init__(self, cfg: DBConfig) -> None:
        self.cfg = cfg

    @staticmethod
    def _is_local_host(host: str | None) -> bool:
        h = str(host or "").strip().lower()
        return h in {"", "_", "localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _socket_candidates() -> list[str]:
        candidates = [
            "/var/run/mysqld/mysqld.sock",
            "/run/mysqld/mysqld.sock",
            "/tmp/mysql.sock",
            "/var/lib/mysql/mysql.sock",
        ]
        out: list[str] = []
        for raw in candidates:
            p = Path(raw)
            if p.exists() and p.is_socket():
                out.append(raw)
        return out

    def _connect_variants(self) -> list[dict[str, Any]]:
        base: dict[str, Any] = {
            "user": self.cfg.user,
            "password": self.cfg.password,
            "database": self.cfg.name,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": 5,
            # Some SQL-driven segment rebuild rules can run for minutes on very
            # large page_hits/lead tables. Keep DB socket alive long enough to
            # avoid false "Lost connection ... timed out" failures mid-query.
            "read_timeout": 1800,
            "write_timeout": 1800,
        }
        host_raw = str(self.cfg.host or "").strip()
        port = int(self.cfg.port or 3306)
        out: list[dict[str, Any]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()

        def _add(kwargs: dict[str, Any]) -> None:
            key = tuple(sorted((str(k), str(v)) for k, v in kwargs.items()))
            if key in seen:
                return
            seen.add(key)
            out.append(kwargs)

        _add({**base, "host": host_raw or "localhost", "port": port})
        if self._is_local_host(host_raw):
            # Prefer unix socket on local DBs because many hosts have grants
            # only for user@localhost and reject user@127.0.0.1.
            for sock in self._socket_candidates():
                _add({**base, "unix_socket": sock})
            _add({**base, "host": "localhost", "port": port})
            _add({**base, "host": "127.0.0.1", "port": port})
        return out

    def _connect(self) -> pymysql.connections.Connection:
        # Bound DB operations so one stuck query/socket cannot freeze loops.
        # Local DB auth layouts differ (localhost vs 127.0.0.1 grants), so we
        # try safe local fallbacks (including unix socket) before failing.
        last_error: Exception | None = None
        for kwargs in self._connect_variants():
            try:
                return pymysql.connect(**kwargs)
            except Exception as e:
                last_error = e
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("mysql_connection_failed")

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

    def rebuild_segment_membership(
        self,
        *,
        segment_id: int,
        select_query_template: str,
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Rebuild one segment directly in DB:
        - execute provided SELECT (must expose `lead_id` column)
        - replace rows in <prefix>lead_lists_leads for this segment
        - update <prefix>lead_lists build metadata
        """
        select_query = self._render_query(select_query_template, context=context).strip()
        if select_query.endswith(";"):
            select_query = select_query[:-1].strip()
        if not select_query:
            raise ValueError("empty_select_query")

        prefix = str(self.cfg.table_prefix or "")
        table_segment_links = self._safe_table(f"{prefix}lead_lists_leads")
        table_segments = self._safe_table(f"{prefix}lead_lists")
        temp_table = "mcd_tmp_segment_leads"
        sid = int(segment_id)
        started = time.monotonic()

        with self._connect() as conn:
            conn.autocommit(False)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TEMPORARY TABLE IF NOT EXISTS `{temp_table}` ("
                        " `lead_id` BIGINT UNSIGNED NOT NULL,"
                        " PRIMARY KEY (`lead_id`)"
                        ") ENGINE=InnoDB"
                    )
                    cur.execute(f"TRUNCATE TABLE `{temp_table}`")
                    cur.execute(
                        "INSERT IGNORE INTO `{tmp}` (`lead_id`) "
                        "SELECT DISTINCT CAST(src.lead_id AS UNSIGNED) "
                        "FROM ({sel}) src "
                        "WHERE src.lead_id IS NOT NULL".format(tmp=temp_table, sel=select_query)
                    )
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM `{temp_table}`")
                    row = cur.fetchone()
                    selected_count = int((row or {}).get("cnt") or 0)

                    cur.execute(f"DELETE FROM `{table_segment_links}` WHERE `leadlist_id`=%s", (sid,))
                    deleted_count = int(cur.rowcount or 0)
                    cur.execute(
                        "INSERT INTO `{tbl}` "
                        "(`leadlist_id`,`lead_id`,`date_added`,`manually_removed`,`manually_added`) "
                        "SELECT %s, t.lead_id, NOW(), 0, 0 FROM `{tmp}` t".format(
                            tbl=table_segment_links,
                            tmp=temp_table,
                        ),
                        (sid,),
                    )
                    inserted_count = int(cur.rowcount or 0)
                    elapsed_sec = max(0.01, float(time.monotonic() - started))

                    # Mautic treats a segment as needing rebuild when
                    # date_modified >= last_built_date. Keep the real
                    # definition-modified timestamp if present, but guarantee
                    # the built marker is strictly newer than it.
                    cur.execute(
                        f"UPDATE `{table_segments}` "
                        "SET "
                        " `checked_out`=NULL,"
                        " `checked_out_by`=NULL,"
                        " `checked_out_by_user`=NULL,"
                        " `date_modified`=CASE "
                        "   WHEN `date_modified` IS NULL OR `date_modified` >= NOW() "
                        "   THEN DATE_SUB(NOW(), INTERVAL 1 SECOND) "
                        "   ELSE `date_modified` "
                        " END,"
                        " `last_built_date`=NOW(),"
                        " `last_built_time`=%s "
                        "WHERE `id`=%s",
                        (elapsed_sec, sid),
                    )
                    cur.execute(f"DROP TEMPORARY TABLE IF EXISTS `{temp_table}`")
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    conn.autocommit(True)
                except Exception:
                    pass

        return {
            "segment_id": sid,
            "selected_count": selected_count,
            "deleted_count": deleted_count,
            "inserted_count": inserted_count,
            "duration_sec": max(0.0, float(time.monotonic() - started)),
        }

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

    def fetch_processlist(self, *, limit: int = 500) -> list[dict[str, Any]]:
        query = "SHOW FULL PROCESSLIST"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            if len(out) >= max(1, int(limit)):
                break
        return out
