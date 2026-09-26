"""Typed, read-only database observations for catalog applicability.

This primitive is not a released executor capability until selected-instance
binding, admission/apply drift guards and complete rollback barriers consume it.
Catalog data supplies identifiers and values, never executable SQL.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SCHEMA = "mcd-mautic-patch-facts-v1"
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_FQCN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\\[A-Za-z_][A-Za-z0-9_]*)+\Z")
_INT_TYPES = frozenset({"tinyint", "smallint", "mediumint", "int", "bigint"})
_TEXT_TYPES = frozenset({"char", "varchar", "tinytext", "text", "mediumtext", "longtext"})


class PatchFactsError(ValueError):
    """An observation cannot establish a verified applicability fact."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise PatchFactsError("fact_identifier_invalid")
    return value


def _table(prefix: Any, suffix: Any) -> str:
    if not isinstance(prefix, str) or (prefix and not _IDENT.fullmatch(prefix)):
        raise PatchFactsError("fact_table_prefix_unknown")
    return _identifier(prefix + _identifier(suffix))


def validate_predicates(predicates: Any) -> None:
    if not isinstance(predicates, list) or not predicates or len(predicates) > 32:
        raise PatchFactsError("fact_predicates_invalid")
    domains: dict[tuple[str, str], tuple[int, ...]] = {}
    for row in predicates:
        if not isinstance(row, dict):
            raise PatchFactsError("fact_predicate_not_object")
        kind = row.get("kind")
        _identifier(row.get("table_suffix"))
        if kind == "column_integer_domain":
            if set(row) != {"kind", "table_suffix", "column", "allowed_values"}:
                raise PatchFactsError("fact_domain_fields_invalid")
            column = _identifier(row["column"])
            values = row["allowed_values"]
            if (not isinstance(values, list) or not 1 <= len(values) <= 16
                    or any(type(value) is not int or abs(value) > 2**63 - 1 for value in values)
                    or values != sorted(set(values))):
                raise PatchFactsError("fact_domain_values_invalid")
            key = row["table_suffix"], column
            if key in domains:
                raise PatchFactsError("fact_domain_duplicate")
            domains[key] = tuple(values)
        elif kind == "table_row_count":
            if (set(row) != {"kind", "table_suffix", "filters", "comparison", "value"}
                    or row["comparison"] not in {"equal", "greater_than"}
                    or type(row["value"]) is not int or not 0 <= row["value"] <= 2**63 - 1
                    or not isinstance(row["filters"], list) or len(row["filters"]) > 8):
                raise PatchFactsError("fact_count_fields_invalid")
            seen: set[str] = set()
            for item in row["filters"]:
                if (not isinstance(item, dict) or set(item) != {"column", "comparison", "value"}
                        or item["comparison"] != "equal" or type(item["value"]) is not int):
                    raise PatchFactsError("fact_filter_invalid")
                column = _identifier(item["column"])
                if column in seen:
                    raise PatchFactsError("fact_filter_duplicate")
                seen.add(column)
        elif kind == "migration_execution_state":
            if (set(row) != {"kind", "table_suffix", "version_column", "migration", "encoding", "expected"}
                    or row["encoding"] != "fqcn_utf8" or row["expected"] not in {"pending", "executed"}
                    or not isinstance(row["migration"], str) or not _FQCN.fullmatch(row["migration"])
                    or len(row["migration"].encode("utf-8")) > 1024):
                raise PatchFactsError("fact_migration_fields_invalid")
            _identifier(row["version_column"])
        else:
            raise PatchFactsError("fact_kind_unsupported")
    # Counting a filtered column is allowed only after its complete domain has
    # been established. A malformed or nullable row never disappears in WHERE.
    for row in predicates:
        if row["kind"] == "table_row_count":
            for item in row["filters"]:
                values = domains.get((row["table_suffix"], item["column"]))
                if values is None or item["value"] not in values:
                    raise PatchFactsError("fact_filter_domain_required")


def migration_state(values: list[Any], migration: str) -> tuple[str, int]:
    if len(values) > 10000:
        raise PatchFactsError("fact_migration_storage_limit")
    count = 0
    for value in values:
        if (not isinstance(value, str) or not _FQCN.fullmatch(value)
                or len(value.encode("utf-8")) > 1024):
            raise PatchFactsError("fact_migration_encoding_unknown")
        if value != migration and value.casefold() == migration.casefold():
            raise PatchFactsError("fact_migration_case_ambiguous")
        count += int(value.encode("utf-8") == migration.encode("utf-8"))
    if count > 1:
        raise PatchFactsError("fact_migration_cardinality_unknown")
    return ("executed" if count == 1 else "pending"), count


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise PatchFactsError("fact_count_value_unknown")
    return value


class ReadOnlyObserver:
    """Consume a DB-API DictCursor connection in a fresh read-only transaction.

    The connection and resolved prefix are supplied by the independent instance
    discovery layer, not the catalog. The caller owns closing the connection.
    No DDL, data repair, storage initialization or migration is executed.
    """

    def __init__(self, connection: Any, table_prefix: str):
        _table(table_prefix, "binding_probe")
        self.connection = connection
        self.prefix = table_prefix

    def _column(self, cursor: Any, table: str, column: str) -> dict[str, Any]:
        cursor.execute(
            "SELECT c.DATA_TYPE AS data_type, c.COLUMN_TYPE AS column_type, "
            "c.IS_NULLABLE AS nullable, c.CHARACTER_SET_NAME AS charset, "
            "c.COLLATION_NAME AS collation, t.ENGINE AS engine "
            "FROM information_schema.COLUMNS c JOIN information_schema.TABLES t "
            "ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME "
            "WHERE c.TABLE_SCHEMA=DATABASE() AND BINARY c.TABLE_NAME=BINARY %s "
            "AND BINARY c.COLUMN_NAME=BINARY %s", (table, column),
        )
        rows = cursor.fetchall()
        if len(rows) != 1 or str(rows[0].get("engine", "")).upper() != "INNODB":
            raise PatchFactsError("fact_schema_missing_or_nontransactional")
        return dict(rows[0])

    def observe(self, predicates: list[dict[str, Any]]) -> dict[str, Any]:
        validate_predicates(predicates)
        rows: dict[int, dict[str, Any]] = {}
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute("SELECT DATABASE() AS database_name, VERSION() AS server_version, "
                               "@@hostname AS server_name, @@port AS server_port")
                identity = cursor.fetchone()
                if not isinstance(identity, dict) or not all(identity.get(key) is not None
                        for key in ("database_name", "server_version", "server_name", "server_port")):
                    raise PatchFactsError("fact_database_identity_unknown")
                identity = dict(identity, driver="mysql_protocol")
                # Verify all domain facts first, irrespective of plan ordering.
                order = sorted(range(len(predicates)), key=lambda i: predicates[i]["kind"] != "column_integer_domain")
                for index in order:
                    predicate = predicates[index]
                    table = _table(self.prefix, predicate["table_suffix"])
                    kind = predicate["kind"]
                    observation: dict[str, Any] = {"predicate": predicate}
                    if kind == "column_integer_domain":
                        column = _identifier(predicate["column"])
                        metadata = self._column(cursor, table, column)
                        if metadata["data_type"] not in _INT_TYPES:
                            raise PatchFactsError("fact_column_integer_type_unknown")
                        marks = ",".join("%s" for _ in predicate["allowed_values"])
                        cursor.execute(
                            f"SELECT COUNT(*) AS total, COALESCE(SUM(`{column}` IS NULL),0) AS null_count, "
                            f"COALESCE(SUM(`{column}` IS NOT NULL AND `{column}` NOT IN ({marks})),0) AS invalid_count "
                            f"FROM `{table}`", tuple(predicate["allowed_values"]),
                        )
                        value = cursor.fetchone()
                        # SUM returns Decimal in real MySQL/MariaDB; normalize
                        # only verified integral values, never strings/bools.
                        from decimal import Decimal
                        counts = {}
                        for key in ("total", "null_count", "invalid_count"):
                            item = value[key]
                            if isinstance(item, Decimal) and item.is_finite() and item == item.to_integral_value():
                                item = int(item)
                            counts[key] = _count(item)
                        if counts["null_count"] or counts["invalid_count"]:
                            raise PatchFactsError("fact_column_domain_unknown")
                        observation.update(observed=counts, metadata=metadata, matched=True)
                    elif kind == "table_row_count":
                        for item in predicate["filters"]:
                            self._column(cursor, table, item["column"])
                        # Even an unfiltered count must prove a transactional table.
                        cursor.execute("SELECT ENGINE AS engine FROM information_schema.TABLES "
                                       "WHERE TABLE_SCHEMA=DATABASE() AND BINARY TABLE_NAME=BINARY %s", (table,))
                        tables = cursor.fetchall()
                        if len(tables) != 1 or str(tables[0].get("engine", "")).upper() != "INNODB":
                            raise PatchFactsError("fact_schema_missing_or_nontransactional")
                        terms = [f"`{item['column']}`=%s" for item in predicate["filters"]]
                        where = " WHERE " + " AND ".join(terms) if terms else ""
                        cursor.execute(f"SELECT COUNT(*) AS actual FROM `{table}`{where}",
                                       tuple(item["value"] for item in predicate["filters"]))
                        actual = _count(cursor.fetchone()["actual"])
                        matched = actual == predicate["value"] if predicate["comparison"] == "equal" else actual > predicate["value"]
                        observation.update(observed=actual, matched=matched)
                    else:
                        column = _identifier(predicate["version_column"])
                        metadata = self._column(cursor, table, column)
                        if metadata["data_type"] not in _TEXT_TYPES or metadata["charset"] not in {"utf8", "utf8mb3", "utf8mb4"}:
                            raise PatchFactsError("fact_migration_column_encoding_unknown")
                        cursor.execute(f"SELECT `{column}` AS version FROM `{table}` LIMIT 10001")
                        values = [item["version"] for item in cursor.fetchall()]
                        state, cardinality = migration_state(values, predicate["migration"])
                        observation.update(observed=state, exact_match_count=cardinality,
                                           storage_sha256=digest(sorted(values)), metadata=metadata,
                                           matched=state == predicate["expected"])
                    rows[index] = observation
            ordered = [rows[index] for index in range(len(predicates))]
            return {"schema": SCHEMA, "database_identity": identity,
                    "database_identity_sha256": digest(identity), "table_prefix": self.prefix,
                    "facts": ordered, "all_matched": all(row["matched"] for row in ordered)}
        except PatchFactsError:
            raise
        except Exception as exc:
            # DB exception messages may contain connection/user identifiers.
            # Return a stable nonsecret diagnostic rather than leaking details.
            raise PatchFactsError("fact_database_observation_failed") from exc
        finally:
            self.connection.rollback()


def bind_evidence(observation: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    fields = {"instance_uid", "application_root", "table_prefix", "source_version", "target_version",
              "run_id", "plan_sha256", "trigger", "phase", "operation"}
    if set(context) != fields or context["table_prefix"] != observation.get("table_prefix"):
        raise PatchFactsError("fact_execution_context_invalid")
    if (not isinstance(context["instance_uid"], str) or not context["instance_uid"]
            or not isinstance(context["application_root"], str) or not context["application_root"].startswith("/")
            or not re.fullmatch(r"[0-9a-f]{64}", str(context["plan_sha256"]))):
        raise PatchFactsError("fact_execution_context_invalid")
    binding = {"schema": SCHEMA, "execution_context": context, "observation": observation}
    return dict(binding, facts_sha256=digest(binding))


def require_unchanged(accepted: dict[str, Any], current: dict[str, Any]) -> None:
    if accepted != current or accepted.get("facts_sha256") != digest({key: value for key, value in accepted.items() if key != "facts_sha256"}):
        raise PatchFactsError("fact_execution_context_or_values_drift")
