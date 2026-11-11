"""Concurrency tests for the DuckDB query engine.

The server shares one QueryEngine (one DuckDB connection) between tool calls
running in different threads. Without serialising access, a ``load``
registering tables while a ``query`` scans a pandas DataFrame either failed
with DuckDB's "connection is busy with another call" error or deadlocked on
the GIL vs. DuckDB's connection mutex, freezing the whole process. The stress
test therefore runs in a subprocess with a hard timeout.
"""

import subprocess
import sys
import textwrap
import threading
import time

import pandas as pd

from mcap_mcp_server.query_engine import QueryEngine

_STRESS = textwrap.dedent(
    """
    import threading, time
    import numpy as np, pandas as pd
    from mcap_mcp_server.query_engine import QueryEngine

    eng = QueryEngine(query_timeout_s=30, max_memory_mb=4096)
    n = 300_000
    eng.register_dataframe(
        "base", pd.DataFrame({"a": np.arange(n), "s": ["x%d" % i for i in range(n)]})
    )
    stop = time.time() + 15
    errors = []

    def querier():
        try:
            while time.time() < stop:
                r = eng.execute("SELECT count(*), max(length(s)) FROM base")
                assert "error" not in r, r
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def registrar(k):
        i = 0
        try:
            while time.time() < stop:
                df = pd.DataFrame({"a": np.arange(5000), "s": ["y"] * 5000})
                eng.register_dataframe(f"t{k}_{i % 50}", df, group=f"g{k}_{i % 50}")
                i += 1
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=querier) for _ in range(3)]
    ts += [threading.Thread(target=registrar, args=(k,)) for k in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    print("OK")
    """
)


def test_concurrent_register_and_query_do_not_deadlock():
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _STRESS],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "register_dataframe + execute deadlocked (stress run did not finish in 90 s)"
        ) from None
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout


def test_timeout_while_waiting_for_connection_returns():
    """A query queued behind a busy connection must time out on schedule
    instead of blocking until the connection frees up."""
    eng = QueryEngine(query_timeout_s=1)
    try:
        eng.register_dataframe("t", pd.DataFrame({"a": [1, 2, 3]}))
        held = threading.Event()
        release = threading.Event()

        def hold_connection():
            with eng._lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold_connection)
        holder.start()
        assert held.wait(5)

        start = time.monotonic()
        result = eng.execute("SELECT * FROM t")
        elapsed = time.monotonic() - start
        release.set()
        holder.join(5)

        assert "timed out" in result["error"]
        assert elapsed < 5

        # Connection is usable again afterwards.
        assert eng.execute("SELECT count(*) FROM t")["rows"] == [[3]]
    finally:
        eng.close()
