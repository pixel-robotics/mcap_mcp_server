"""DuckDB query engine: table registration, SQL execution, and safety enforcement."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

_BLOCKED_FUNCTIONS = [
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_json_objects",
]

_BLOCKED_KEYWORDS = [
    "COPY",
    "EXPORT",
    "IMPORT",
    "ATTACH",
    "LOAD",
    "INSTALL",
]


class _QueryTimeout(Exception):
    """Raised internally when a query exceeds the configured timeout."""


class QueryEngine:
    """In-memory DuckDB engine for querying DataFrames registered as virtual tables."""

    def __init__(
        self,
        query_timeout_s: int = 30,
        default_row_limit: int = 1000,
        max_row_limit: int = 10000,
        max_memory_mb: int = 2048,
    ) -> None:
        self._conn = duckdb.connect(database=":memory:")
        # One DuckDB connection is shared by every thread (tool calls run in
        # AnyIO worker threads, loads in the asyncio executor). All access goes
        # through this lock: otherwise a register() that holds the GIL while
        # waiting for DuckDB's connection mutex deadlocks against a query that
        # holds the mutex and needs the GIL to scan a pandas DataFrame, and the
        # whole server (event loop included) stops responding.
        self._lock = threading.RLock()
        self._query_timeout_s = query_timeout_s
        self._default_row_limit = default_row_limit
        self._max_row_limit = max_row_limit
        self._max_memory_bytes = max_memory_mb * 1024 * 1024
        self._tables: dict[str, int] = {}  # table_name -> row_count
        self._table_memory: dict[str, int] = {}  # table_name -> bytes
        # group -> list of table names, ordered by insertion time (LRU)
        self._groups: OrderedDict[str, list[str]] = OrderedDict()
        self._last_evicted: list[str] = []
        self._configure_safety()

    def _configure_safety(self) -> None:
        """Lock down DuckDB to prevent file access and modifications."""
        try:
            self._conn.execute("SET enable_external_access = false")
        except duckdb.Error:
            logger.debug("Could not disable external access (older DuckDB version)")
        try:
            self._conn.execute("SET lock_configuration = true")
        except duckdb.Error:
            pass

    def register_dataframe(
        self, name: str, df: pd.DataFrame, group: str = "_default"
    ) -> int:
        """Register a pandas DataFrame as a queryable DuckDB table.

        *group* ties tables together for LRU eviction (typically the
        recording alias or filename).  Returns the number of rows registered.
        """
        mem = int(df.memory_usage(deep=True).sum())
        with self._lock:
            self._evict_if_needed(mem)

            self._conn.register(name, df)
            row_count = len(df)
            self._tables[name] = row_count
            self._table_memory[name] = mem

            if group not in self._groups:
                self._groups[group] = []
            self._groups[group].append(name)
            self._groups.move_to_end(group)

        logger.info("Registered table %r (%d rows, %d cols)", name, row_count, len(df.columns))
        return row_count

    @property
    def total_memory_bytes(self) -> int:
        return sum(self._table_memory.values())

    def _evict_if_needed(self, incoming_bytes: int) -> None:
        """Evict oldest groups until there is room for *incoming_bytes*."""
        while (
            self._groups
            and self.total_memory_bytes + incoming_bytes > self._max_memory_bytes
        ):
            oldest_group, tables = self._groups.popitem(last=False)
            for t in list(tables):
                self._last_evicted.append(t)
                self._do_unregister(t)
            logger.info("LRU-evicted group %r (%d tables)", oldest_group, len(tables))

    def drain_evicted(self) -> list[str]:
        """Return and clear the list of tables evicted since last drain."""
        evicted = self._last_evicted
        self._last_evicted = []
        return evicted

    def unregister(self, name: str) -> None:
        with self._lock:
            self._do_unregister(name)
            for group, tables in self._groups.items():
                if name in tables:
                    tables.remove(name)
                    break

    def _do_unregister(self, name: str) -> None:
        try:
            with self._lock:
                self._conn.unregister(name)
        except duckdb.Error:
            pass
        self._tables.pop(name, None)
        self._table_memory.pop(name, None)

    def drop_tables_with_prefix(self, prefix: str) -> list[str]:
        """Remove all tables whose names start with *prefix*."""
        to_drop = [t for t in self._tables if t.startswith(prefix)]
        for t in to_drop:
            self.unregister(t)
        return to_drop

    def list_tables(self) -> dict[str, int]:
        """Return a mapping of table_name -> row_count for all registered tables."""
        return dict(self._tables)

    def execute(
        self,
        sql: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Execute a read-only SQL query with safety checks.

        Returns a dict matching the spec's query response shape.
        """
        self._check_sql_safety(sql)

        effective_limit = min(
            limit if limit is not None else self._default_row_limit,
            self._max_row_limit,
        )

        limited_sql = sql.rstrip().rstrip(";")
        has_limit = "LIMIT" in limited_sql.upper().split("--")[0]
        if not has_limit:
            limited_sql = f"{limited_sql} LIMIT {effective_limit + 1}"

        start = time.monotonic()
        try:
            description, rows = self._execute_with_timeout(limited_sql)
        except _QueryTimeout:
            return {
                "error": f"Query timed out after {self._query_timeout_s}s",
                "execution_time_ms": _elapsed_ms(start),
            }
        except duckdb.Error as e:
            return {"error": str(e), "execution_time_ms": _elapsed_ms(start)}

        elapsed = _elapsed_ms(start)
        truncated = len(rows) > effective_limit
        if truncated:
            rows = rows[:effective_limit]

        columns = [desc[0] for desc in description] if description else []
        types = [desc[1] for desc in description] if description else []

        return {
            "columns": columns,
            "types": types,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "execution_time_ms": elapsed,
        }

    def _execute_with_timeout(self, sql: str) -> tuple[list, list]:
        """Run a SQL statement with a timeout, interrupting DuckDB if exceeded."""

        state = {"started": False, "abandoned": False}

        def _run() -> tuple[list, list]:
            with self._lock:
                if state["abandoned"]:  # timed out while waiting for the lock
                    return [], []
                state["started"] = True
                result = self._conn.execute(sql)
                return result.description, result.fetchall()

        # No `with` block: its __exit__ would wait for a timed-out query to
        # finish, so the timeout would never actually return to the caller.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_run)
            try:
                return future.result(timeout=self._query_timeout_s)
            except concurrent.futures.TimeoutError:
                state["abandoned"] = True
                # Only interrupt if *this* query holds the connection, not one
                # queued behind it. interrupt() just sets a flag; no lock needed.
                if state["started"]:
                    self._conn.interrupt()
                raise _QueryTimeout
        finally:
            pool.shutdown(wait=False)

    def _check_sql_safety(self, sql: str) -> None:
        upper = sql.upper()
        for kw in _BLOCKED_KEYWORDS:
            tokens = upper.split()
            if kw in tokens:
                raise ValueError(f"Blocked SQL keyword: {kw}")

        for fn in _BLOCKED_FUNCTIONS:
            if fn.upper() + "(" in upper.replace(" ", ""):
                raise ValueError(f"Blocked SQL function: {fn}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
