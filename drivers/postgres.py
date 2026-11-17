"""PostgreSQL driver (psycopg v3) — one read, always inside a READ ONLY tx.

Implements the uniform driver interface consumed by the query cycle:

* :func:`connect`    -> client (``sslmode``/``connect_timeout`` from options);
* :func:`execute`    -> ``{rows, row_count, truncated, server_version}``;
* :func:`probe`      -> ``{server_version}`` (used by ``test_connection``);
* :func:`close`      -> release the client, never raising.

Read safety is defence in depth (spec ``query-policy``): besides the filter in
the tool, the driver runs inside a transaction declared ``READ ONLY`` (task
1.2), which is what actually stops forms the filter does not see — ``WITH ...
DELETE``, ``SELECT ... INTO``, functions with side effects — because the *bank*
refuses them. Cleanup is guaranteed: whatever happens, the transaction is
rolled back and the cursor closed (no connection left open on the server).

The module talks to ``psycopg`` **lazily**: importing ``drivers.postgres`` never
imports psycopg, so a host without the dependency keeps working (R1) and the
tests inject a fake module (real instances still pending — host ``hostgator``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from drivers import DriverMissingError, install_command

ENGINE = "postgres"
DRIVER_MODULE = "psycopg"

#: Probe used by ``test_connection`` (single statement, no table involved).
PROBE_STATEMENT = "SELECT 1"

#: Sane defaults when the connection options do not override them (mirrors
#: ``service.DEFAULT_*``; re-declared here on purpose so the driver stays
#: importable without the service layer).
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_STATEMENT_TIMEOUT_S = 30
DEFAULT_MAX_ROWS = 200

#: Applied inside the transaction, before the statement itself.
READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"

#: Attribute where :func:`connect` remembers the connection's statement timeout
#: so :func:`execute` honours it even when the caller does not pass it.
TIMEOUT_ATTR = "_hermes_statement_timeout_s"

_MISSING = object()
_psycopg: Any = _MISSING


def set_driver_module(module: Any) -> None:
    """Inject the driver module (tests/fakes) and drop any cached one."""
    global _psycopg
    _psycopg = module


def forget_driver_module() -> None:
    """Force the next :func:`_driver` call to re-import."""
    global _psycopg
    _psycopg = _MISSING


def _driver() -> Any:
    """Return the psycopg module, importing it here (never at module load)."""
    global _psycopg
    if _psycopg is _MISSING:
        try:
            import psycopg as module  # type: ignore[import-not-found]
        except ImportError:
            raise DriverMissingError(
                ENGINE,
                DRIVER_MODULE,
                install_command(ENGINE),
                f"driver '{DRIVER_MODULE}' for engine '{ENGINE}' is not installed",
            ) from None
        _psycopg = module
    return _psycopg


# --------------------------------------------------------------------------
# options helpers (self-contained: importing ``service`` here would close a
# cycle, since the service imports ``drivers``)
# --------------------------------------------------------------------------


def options_of(connection_row: Mapping[str, Any]) -> dict[str, Any]:
    """Decode ``options_json`` (string) or ``options`` (mapping/``None``)."""
    if not isinstance(connection_row, Mapping):
        return {}
    raw = connection_row.get("options_json")
    if raw is None:
        raw = connection_row.get("options")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def connect_timeouts(connection_row: Mapping[str, Any]) -> tuple[float, float]:
    """``(connect_timeout_s, statement_timeout_s)`` for the connection."""
    options = options_of(connection_row)
    return (
        _positive_float(options.get("connect_timeout_s"), DEFAULT_CONNECT_TIMEOUT_S),
        _positive_float(options.get("statement_timeout_s"), DEFAULT_STATEMENT_TIMEOUT_S),
    )


# --------------------------------------------------------------------------
# uniform interface
# --------------------------------------------------------------------------


def connect(connection_row: Mapping[str, Any], secret_value: str | None = None) -> Any:
    """Open a client for ``connection_row`` (no implicit transaction started).

    ``sslmode`` and the TLS material come straight from the connection options;
    ``connect_timeout`` is passed to the driver as whole seconds (psycopg).
    """
    row = dict(connection_row) if isinstance(connection_row, Mapping) else {}
    options = options_of(row)
    connect_timeout_s, statement_timeout_s = connect_timeouts(row)

    kwargs: dict[str, Any] = {
        "host": str(row.get("host") or ""),
        "connect_timeout": max(int(connect_timeout_s), 1),
    }
    if row.get("port"):
        kwargs["port"] = _positive_int(row.get("port"), 0)
    if row.get("database"):
        kwargs["dbname"] = str(row["database"])
    if row.get("username"):
        kwargs["user"] = str(row["username"])
    if secret_value is not None:
        kwargs["password"] = secret_value

    # TLS: pass through whatever the connection declared, nothing invented.
    for key in ("sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl", "gssencmode"):
        if options.get(key):
            kwargs[key] = options[key]
    if options.get("application_name"):
        kwargs["application_name"] = str(options["application_name"])

    client = _driver().connect(**kwargs)
    _remember_statement_timeout(client, statement_timeout_s)
    return client


def execute(
    client: Any,
    statement: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    *,
    statement_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Run **one** statement inside a READ ONLY transaction and fetch at most
    ``max_rows`` rows.

    Returns ``{rows, row_count, truncated, server_version}``. The rollback and
    the cursor close happen even when the statement raises, so no connection is
    left in an open transaction on the server (spec query-policy: the bank is
    the one that refuses the disguised write).

    ``statement_timeout_s`` defaults to :data:`DEFAULT_STATEMENT_TIMEOUT_S`;
    the query cycle passes the connection's option when it has it (uniform
    signature across engines — Redis included).
    """
    limit = _positive_int(max_rows, DEFAULT_MAX_ROWS)
    cursor = client.cursor()
    try:
        _read_only_transaction(client, cursor, statement_timeout_for(client, statement_timeout_s))
        started = time.perf_counter()
        cursor.execute(statement)
        rows, fetched = _fetch_limited(cursor, limit)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        return {
            "rows": rows,
            "row_count": len(rows),
            "truncated": fetched > limit,
            "duration_ms": duration_ms,
            "server_version": server_version(client),
        }
    finally:
        _cleanup(client, cursor)


def probe(client: Any, statement: str = PROBE_STATEMENT) -> dict[str, Any]:
    """Minimal probe for ``test_connection``: one statement, no rows needed."""
    cursor = client.cursor()
    try:
        cursor.execute(statement)
        try:
            cursor.fetchone()
        except Exception:  # noqa: BLE001 - drivers without a result set
            pass
        version = server_version(client)
        return {"server_version": version} if version else {}
    finally:
        _close_quietly(cursor)


def close(client: Any) -> None:
    """Release the client, never raising (used after any error path)."""
    _close_quietly(client)


def server_version(client: Any) -> str | None:
    """Best-effort version read **while the client is open** (never raises)."""
    for holder_name in ("info", None):
        try:
            holder = client if holder_name is None else getattr(client, holder_name)
            value = getattr(holder, "server_version", None) if holder is not None else None
        except Exception:  # noqa: BLE001 - closed/blocked client: report nothing
            continue
        if isinstance(value, (str, int, float)) and value:
            return str(value)
    return None


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _remember_statement_timeout(client: Any, seconds: float) -> None:
    """Keep the connection's statement timeout on the client (never raises)."""
    try:
        setattr(client, TIMEOUT_ATTR, float(seconds))
    except Exception:  # noqa: BLE001 - read-only/slotted client: fall back silently
        pass


def statement_timeout_for(client: Any, explicit: float | None = None) -> float:
    """Timeout to apply: explicit > remembered at connect > documented default."""
    if explicit is not None:
        return _positive_float(explicit, DEFAULT_STATEMENT_TIMEOUT_S)
    return _positive_float(getattr(client, TIMEOUT_ATTR, None), DEFAULT_STATEMENT_TIMEOUT_S)


def _read_only_transaction(client: Any, cursor: Any, statement_timeout_s: float | None = None) -> None:
    """Declare the transaction READ ONLY and enforce the statement timeout.

    Order matters: psycopg opens the transaction on the first execute, so the
    statement timeout is set first and ``SET TRANSACTION READ ONLY`` lands in
    the same (still empty) transaction.
    """
    if statement_timeout_s is None:
        statement_timeout_s = DEFAULT_STATEMENT_TIMEOUT_S
    try:
        # Value interpolated as an int: no placeholder, no injection surface.
        cursor.execute(f"SET statement_timeout = {max(int(round(statement_timeout_s * 1000)), 1)}")
    except Exception:  # noqa: BLE001 - unsupported by a fake/driver: keep going
        pass
    cursor.execute(READ_ONLY_STATEMENT)


def _fetch_limited(cursor: Any, limit: int) -> tuple[list[list[Any]], int]:
    """``(rows, fetched)`` — at most ``limit`` rows, plus the raw fetch size.

    Truncation comes from that raw size and never from ``cursor.rowcount``
    (which is ``-1`` in psycopg v3 for a plain ``SELECT``): exactly ``limit``
    rows and nothing else in the cursor is **not** truncated.
    """
    try:
        fetched = list(cursor.fetchmany(limit + 1) or ())  # +1 detects truncation
    except Exception:  # noqa: BLE001 - driver without fetchmany
        fetched = list(cursor.fetchall() or ())
    rows: list[list[Any]] = []
    for row in fetched:
        rows.append(list(row) if not isinstance(row, (str, bytes)) else [row])
    return rows[:limit], len(fetched)


def _cleanup(client: Any, cursor: Any) -> None:
    """Rollback + close: whatever happened, the transaction does not survive."""
    for rollback in (getattr(client, "rollback", None), getattr(cursor, "close", None)):
        if callable(rollback):
            try:
                rollback()
            except Exception:  # noqa: BLE001 - cleanup masks nothing
                pass


def _close_quietly(obj: Any) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
