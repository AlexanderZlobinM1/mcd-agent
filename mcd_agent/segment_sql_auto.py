from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import phpserialize


_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_URL_LAST_DAYS_RE = re.compile(r"^(url|url_title)_in_last_(\d+)_days$", re.IGNORECASE)


@dataclass(frozen=True)
class DetectedSQLSegmentRule:
    segment_id: int
    select_sql: str
    depends_on: tuple[int, ...]
    reason: str
    clause_count: int
    has_page_hits: bool
    problem_count: int
    checked_out: bool


@dataclass(frozen=True)
class _CompiledClause:
    sql: str
    has_page_hits: bool


def _safe_ident(name: object) -> str | None:
    raw = str(name or "").strip()
    if not raw or not _SAFE_IDENT_RE.match(raw):
        return None
    return raw


def _sql_quote(value: object) -> str:
    raw = str(value or "")
    raw = raw.replace("\\", "\\\\").replace("'", "''")
    return f"'{raw}'"


def _to_text(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("utf-8", errors="ignore")
    return str(value or "")


def _php_array_values(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _normalize_filter_values(raw: object) -> list[str]:
    out: list[str] = []
    for item in _php_array_values(raw):
        text = _to_text(item).strip()
        if text:
            out.append(text)
    return out


def _decode_filters(filters_raw: str) -> list[dict[str, object]]:
    raw = str(filters_raw or "").strip()
    if not raw:
        return []
    try:
        parsed = phpserialize.loads(raw.encode("utf-8"), decode_strings=True)
    except Exception:
        return []
    rows: list[dict[str, object]] = []
    for item in _php_array_values(parsed):
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _clause_filter_value(clause: dict[str, object]) -> object:
    props = clause.get("properties")
    if isinstance(props, dict) and "filter" in props:
        return props.get("filter")
    return clause.get("filter")


def _compile_like_any(expr: str, values: list[str]) -> str | None:
    uniq = list(dict.fromkeys([v for v in values if v]))
    if not uniq:
        return None
    parts = [f"{expr} LIKE {_sql_quote('%' + value + '%')}" for value in uniq]
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def _compile_not_like_any(expr: str, null_expr: str, values: list[str]) -> str | None:
    uniq = list(dict.fromkeys([v for v in values if v]))
    if not uniq:
        return None
    parts = [f"{expr} NOT LIKE {_sql_quote('%' + value + '%')}" for value in uniq]
    if len(parts) == 1:
        return f"({null_expr} IS NULL OR {parts[0]})"
    return f"({null_expr} IS NULL OR (" + " AND ".join(parts) + "))"


def _compile_lead_clause(clause: dict[str, object]) -> _CompiledClause | None:
    field = _safe_ident(clause.get("field"))
    if not field:
        return None
    operator = str(clause.get("operator") or "").strip().lower()
    col_expr = f"l.`{field}`"
    cast_expr = f"CAST({col_expr} AS CHAR CHARACTER SET utf8mb4)"
    if field == "tags":
        values = _normalize_filter_values(_clause_filter_value(clause))
        tag_ids: list[int] = []
        for value in values:
            try:
                tag_ids.append(int(str(value).strip()))
            except Exception:
                continue
        tag_ids = list(dict.fromkeys([tid for tid in tag_ids if tid > 0]))
        if operator not in {"in", "contains"} or not tag_ids:
            return None
        in_sql = ",".join(str(tid) for tid in tag_ids)
        return _CompiledClause(
            sql=(
                "EXISTS ("
                "SELECT 1 FROM {prefix}lead_tags_xref tx "
                f"WHERE tx.lead_id = l.id AND tx.tag_id IN ({in_sql})"
                ")"
            ),
            has_page_hits=False,
        )

    if operator == "!empty":
        return _CompiledClause(
            sql=f"({col_expr} IS NOT NULL AND {cast_expr} <> '')",
            has_page_hits=False,
        )
    if operator == "empty":
        return _CompiledClause(
            sql=f"({col_expr} IS NULL OR {cast_expr} = '')",
            has_page_hits=False,
        )
    if operator in {"contains", "like"}:
        values = _normalize_filter_values(_clause_filter_value(clause))
        like_sql = _compile_like_any(cast_expr, values)
        if not like_sql:
            return None
        return _CompiledClause(sql=like_sql, has_page_hits=False)
    if operator in {"!like", "!contains", "notlike", "not_contains", "notcontains", "not contains"}:
        values = _normalize_filter_values(_clause_filter_value(clause))
        not_like_sql = _compile_not_like_any(cast_expr, col_expr, values)
        if not not_like_sql:
            return None
        return _CompiledClause(sql=not_like_sql, has_page_hits=False)
    if operator in {"eq", "="}:
        values = _normalize_filter_values(_clause_filter_value(clause))
        if not values:
            return None
        if len(values) == 1:
            return _CompiledClause(sql=f"{cast_expr} = {_sql_quote(values[0])}", has_page_hits=False)
        parts = [f"{cast_expr} = {_sql_quote(value)}" for value in values]
        return _CompiledClause(sql="(" + " OR ".join(parts) + ")", has_page_hits=False)
    return None


def _compile_behavior_clause(clause: dict[str, object]) -> _CompiledClause | None:
    field = str(clause.get("field") or "").strip()
    operator = str(clause.get("operator") or "").strip().lower()
    if operator not in {"contains", "like"}:
        return None
    values = _normalize_filter_values(_clause_filter_value(clause))
    if not values:
        return None

    target_col = None
    days_expr = None
    match = _URL_LAST_DAYS_RE.match(field)
    if match:
        target_col = match.group(1).lower()
        days_expr = f"ph.date_hit >= DATE_SUB('{{now_local}}', INTERVAL {int(match.group(2))} DAY)"
    elif field == "hit_url":
        target_col = "url"
    elif field == "url_title":
        target_col = "url_title"
    if target_col is None:
        return None

    like_sql = _compile_like_any(f"ph.`{target_col}`", values)
    if not like_sql:
        return None
    where_parts = ["ph.lead_id = l.id", like_sql]
    if days_expr:
        where_parts.append(days_expr)
    return _CompiledClause(
        sql=(
            "EXISTS ("
            "SELECT 1 FROM {prefix}page_hits ph "
            "WHERE " + " AND ".join(where_parts) +
            ")"
        ),
        has_page_hits=True,
    )


def _compile_clause(clause: dict[str, object]) -> _CompiledClause | None:
    obj = str(clause.get("object") or "").strip().lower()
    if obj == "lead":
        return _compile_lead_clause(clause)
    if obj == "behaviors":
        return _compile_behavior_clause(clause)
    return None


def _compile_groups(filters_raw: str, *, max_clauses: int) -> tuple[list[str], bool, int] | None:
    clauses = _decode_filters(filters_raw)
    if not clauses or len(clauses) > max_clauses:
        return None

    groups: list[list[str]] = []
    current_group: list[str] = []
    has_page_hits = False
    clause_count = 0
    for idx, clause in enumerate(clauses):
        compiled = _compile_clause(clause)
        if compiled is None:
            return None
        has_page_hits = has_page_hits or compiled.has_page_hits
        clause_count += 1
        glue = str(clause.get("glue") or "").strip().lower()
        if idx > 0 and glue == "or":
            if current_group:
                groups.append(current_group)
            current_group = [compiled.sql]
            continue
        current_group.append(compiled.sql)
    if current_group:
        groups.append(current_group)
    if not groups:
        return None

    group_sql: list[str] = []
    for parts in groups:
        if not parts:
            continue
        if len(parts) == 1:
            group_sql.append(parts[0])
        else:
            group_sql.append("(" + " AND ".join(parts) + ")")
    if not group_sql:
        return None
    return group_sql, has_page_hits, clause_count


def detect_auto_sql_segment_rules(
    segment_rows: list[dict[str, object]],
    *,
    max_clauses: int,
    problem_threshold: int,
) -> dict[int, DetectedSQLSegmentRule]:
    out: dict[int, DetectedSQLSegmentRule] = {}
    for row in segment_rows:
        try:
            sid = int(row.get("id") or 0)
        except Exception:
            continue
        if sid <= 0:
            continue
        compiled = _compile_groups(str(row.get("filters") or ""), max_clauses=max_clauses)
        if compiled is None:
            continue
        groups, has_page_hits, clause_count = compiled
        checked_out = bool(row.get("checked_out"))
        problem_count = int(row.get("problem_count") or 0)
        if not (has_page_hits or checked_out or problem_count >= max(1, problem_threshold)):
            continue
        where_sql = " OR ".join(groups)
        if len(groups) > 1:
            where_sql = "(" + where_sql + ")"
        reason_parts: list[str] = []
        if has_page_hits:
            reason_parts.append("page_hits")
        if checked_out:
            reason_parts.append("checked_out")
        if problem_count >= max(1, problem_threshold):
            reason_parts.append(f"problem_count={problem_count}")
        out[sid] = DetectedSQLSegmentRule(
            segment_id=sid,
            select_sql=(
                "SELECT l.id AS lead_id "
                "FROM {prefix}leads l "
                "WHERE " + where_sql
            ),
            depends_on=(),
            reason=";".join(reason_parts) or "auto",
            clause_count=clause_count,
            has_page_hits=has_page_hits,
            problem_count=problem_count,
            checked_out=checked_out,
        )
    return out
