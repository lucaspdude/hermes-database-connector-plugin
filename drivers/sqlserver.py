"""SQL Server driver — pymssql by default, pyodbc when present.

Same uniform interface as :mod:`drivers.postgres` (``connect``/``execute``/
``probe``/``close``), so the query cycle never knows which engine it is
talking to:

* default path: **pymssql** (FreeTDS) with login/statement timeouts and
  ``tds_version`` from the connection options (task 1.3);
* optional path: **pyodbc**, detected with ``importlib.util.find_spec`` (task
  1.3, design 12 — confirmed for the MVP). Absent -> pymssql, and nothing
  breaks: the two paths never import each other eagerly.
* read safety: ``SET TRANSACTION READ ONLY`` is issued on the connection
  **before** the statement, inside the transaction opened by the driver, and
  that transaction is rolled back at the end of every execution — including
  the error ones (task 1.5, design 9).

Nothing here talks to a server at import time; real-instance validation is
still pending (host ``hostgator``: SQL Server external; the pyodbc path also
needs MS ODBC Driver 18 installed).
"""

from __future__ import annotations

import importlib.util
import json
import time
from collections.abc import Mapping
from typing import Any

from drivers import DriverMissingError, install_command
from drivers.postgres import DEFAULT_MAX_ROWS  # one limit for every engine

ENGINE = "sqlserver"
DRIVER_MODULE = "pymssql"
OPTIONAL_MODULE = "pyodbc"

#: Probe used by ``test_connection``.
PROBE_STATEMENT = "SELECT 1"

DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_STATEMENT_TIMEOUT_S = 30

#: Task 1.5: declared on the connection before the statement runs.
READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"

_MISSING = object()
_pymssql: Any = _MISSING
_pyodbc: Any = _MISSING


# --------------------------------------------------------------------------
# resolução do módulo (pymssql padrão, pyodbc opcional por find_spec)
# --------------------------------------------------------------------------


def set_driver_module(module: Any, *, optional: Any = _MISSING) -> None:
    """Inject pymssql (and optionally pyodbc) — tests and fakes."""
    global _pymssql, _pyodbc
    _pymssql = module
    _pyodbc = optional


def forget_driver_module() -> None:
    """Force the next resolution to re-detect/re-import."""
    global _pymssql, _pyodbc
    _pymssql = _MISSING
    _pyodbc = _MISSING


def _detect(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:  # noqa: BLE001 - broken install counts as absent
        return False


def has_pyodbc() -> bool:
    """True when the optional pyodbc path is available (detection only)."""
    if _pyodbc is not _MISSING:
        return _pyodbc not in (None, False)
    return _detect(OPTIONAL_MODULE)


#: Module-level switch used by the tests to exercise the optional path without
#: installing pyodbc (on a real host the preference comes from ``find_spec``).
_pyodbc_preferred = False


def use_pyodbc(row: Mapping[str, Any] | None = None) -> bool:
    """Whether this connection must go through the optional pyodbc path.

    pymssql is **always** the default (design 5): pyodbc only wins when it is
    detected (``find_spec``) or explicitly asked for via ``options.driver``,
    and the connection did not force the pymssql path.
    """
    forced = options_of(row).get("driver") if isinstance(row, Mapping) else None
    if forced == DRIVER_MODULE:
        return False
    if forced == OPTIONAL_MODULE:
        return True
    if _pyodbc is not _MISSING:
        return _pyodbc_preferred or _pyodbc not in (None, False)
    return _pyodbc_preferred or _detect(OPTIONAL_MODULE)


def _driver(connection_row: Mapping[str, Any] | None = None) -> tuple[str, Any]:
    """``(path, module)`` to use now — ``("pyodbc", module)`` or ``("pymssql", module)``."""
    if use_pyodbc(connection_row):
        module = _pyodbc
        if module is _MISSING or module is None:
            try:
                import pyodbc as module  # type: ignore[import-not-found]  # noqa: PLR0404
            except ImportError:  # pragma: no cover - only when detection lied
                module = None
        if module is not None:
            return OPTIONAL_MODULE, module
    return DRIVER_MODULE, _pymssql_module()


def _pymssql_module() -> Any:
    global _pymssql
    if _pymssql is _MISSING:
        try:
            import pymssql as module  # type: ignore[import-not-found]
        except ImportError:
            raise DriverMissingError(
                ENGINE,
                DRIVER_MODULE,
                install_command(ENGINE),
                f"driver '{DRIVER_MODULE}' for engine '{ENGINE}' is not installed",
            ) from None
        _pymssql = module
    return _pymssql


# --------------------------------------------------------------------------
# options helpers (kept in sync with drivers.postgres — see comment there)
# --------------------------------------------------------------------------


def options_of(connection_row: Mapping[str, Any]) -> dict[str, Any]:
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
    options = options_of(connection_row)
    return (
        _positive_float(options.get("connect_timeout_s"), DEFAULT_CONNECT_TIMEOUT_S),
        _positive_float(options.get("statement_timeout_s"), DEFAULT_STATEMENT_TIMEOUT_S),
    )


# --------------------------------------------------------------------------
# uniform interface
# --------------------------------------------------------------------------


def connect(connection_row: Mapping[str, Any], secret_value: str | None = None) -> Any:
    """Open a client for ``connection_row`` using pymssql or pyodbc.

    pymssql takes ``server`` as ``host:port`` (FreeTDS) and its own timeout
    kwargs; pyodbc takes an ODBC connection string. Both receive login and
    statement timeouts, ``tds_version``/``TrustServerCertificate`` when the
    options declare them.
    """
    row = dict(connection_row) if isinstance(connection_row, Mapping) else {}
    options = options_of(row)
    connect_timeout_s, statement_timeout_s = connect_timeouts(row)
    path, module = _driver(row)

    if path == OPTIONAL_MODULE:
        return _connect_pyodbc(module, row, secret_value, connect_timeout_s, statement_timeout_s)
    return _connect_pymssql(module, row, secret_value, connect_timeout_s, statement_timeout_s)


def _connect_pymssql(
    module: Any,
    row: Mapping[str, Any],
    secret_value: str | None,
    connect_timeout_s: float,
    statement_timeout_s: float,
) -> Any:
    host = str(row.get("host") or "")
    port = row.get("port")
    kwargs: dict[str, Any] = {
        "server": f"{host}:{port}" if port else host,
        "login_timeout": max(int(connect_timeout_s), 1),
        "timeout": max(int(statement_timeout_s), 1),
    }
    if row.get("username"):
        kwargs["user"] = str(row["username"])
    if secret_value is not None:
        kwargs["password"] = secret_value
    if row.get("database"):
        kwargs["database"] = str(row["database"])

    options = options_of(row)
    if options.get("tds_version"):
        kwargs["tds_version"] = str(options["tds_version"])
    if options.get("charset"):
        kwargs["charset"] = str(options["charset"])
    if options.get("appname"):
        kwargs["appname"] = str(options["appname"])

    # FreeTDS has no such kwarg: pymssql raises TypeError for the unknown
    # argument, which we retry without it — the certificate trust of the
    # default path is configured in freetds.conf, not here (native TLS is the
    # reason pyodbc exists as the optional path).
    extra: dict[str, Any] = {}
    if options.get("trust_server_certificate"):
        extra["trust_server_certificate"] = options["trust_server_certificate"]
    if extra:
        try:
            return module.connect(**kwargs, **extra)
        except TypeError:  # noqa: PERF203 - documented pymssql limitation
            pass
    return module.connect(**kwargs)


def _connect_pyodbc(
    module: Any,
    row: Mapping[str, Any],
    secret_value: str | None,
    connect_timeout_s: float,
    statement_timeout_s: float,
) -> Any:
    options = options_of(row)
    parts = [
        "DRIVER={ODBC Driver 18 for SQL Server}",
        f"SERVER={row.get('host') or ''}"
        + (f",{_positive_int(row.get('port'), 0)}" if row.get("port") else ""),
        f"DATABASE={row.get('database') or ''}",
        f"UID={row.get('username') or ''}",
        f"PWD={secret_value or ''}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if options.get('trust_server_certificate') else 'no'}",
        "ConnectRetryCount=0",
        f"TIMEOUT={max(int(connect_timeout_s), 1)}",
    ]
    connection_string = ";".join(parts)
    try:
        client = module.connect(connection_string)
    except TypeError:  # noqa: PERF203 - injected fake with kwargs interface
        client = module.connect(
            connection_string, timeout=max(int(connect_timeout_s), 1), autocommit=False
        )
    _set_query_timeout(client, statement_timeout_s)
    return client


def _set_query_timeout(client: Any, statement_timeout_s: float) -> None:
    """pyodbc ``timeout`` is per connection/query: seconds, never raising."""
    try:
        client.timeout = max(int(statement_timeout_s), 1)
    except Exception:  # noqa: BLE001 - not every build exposes the setter
        pass


def execute(
    client: Any,
    statement: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    *,
    statement_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Run **one** statement with READ ONLY declared on the connection.

    Task 1.5: the transaction is opened by the driver, ``SET TRANSACTION READ
    ONLY`` is issued before the statement and everything is rolled back at the
    end — even when the statement raises — so a disguised write (``WITH ...
    DELETE``) is refused by the bank itself and nothing is left pending.
    """
    limit = _positive_int(max_rows, DEFAULT_MAX_ROWS)
    if statement_timeout_s is not None:
        _set_query_timeout(client, statement_timeout_s)
    cursor = client.cursor()
    try:
        cursor.execute(READ_ONLY_STATEMENT)
        started = time.perf_counter()
        cursor.execute(statement)
        rows = _fetch_limited(cursor, limit)
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        return {
            "rows": rows,
            "row_count": len(rows),
            "truncated": _row_truncated(cursor, limit, len(rows)),
            "duration_ms": duration_ms,
            "server_version": server_version(client),
        }
    finally:
        _cleanup(client, cursor)


def probe(client: Any, statement: str = PROBE_STATEMENT) -> dict[str, Any]:
    """Minimal probe for ``test_connection``."""
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
    _close_quietly(client)


def server_version(client: Any) -> str | None:
    """Best-effort version read while the client is open (never raises)."""
    for holder_name in ("info", None):
        try:
            holder = client if holder_name is None else getattr(client, holder_name)
            value = getattr(holder, "server_version", None) if holder is not None else None
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, (str, int, float)) and value:
            return str(value)
    return None


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _fetch_limited(cursor: Any, limit: int) -> list[list[Any]]:
    try:
        fetched = cursor.fetchmany(limit + 1)
    except Exception:  # noqa: BLE001 - fallback for drivers without fetchmany
        fetched = cursor.fetchall()
    rows: list[list[Any]] = []
    for row in fetched or ():
        rows.append(list(row) if not isinstance(row, (str, bytes)) else [row])
    return rows[:limit]


def _row_truncated(cursor: Any, limit: int, delivered: int) -> bool:
    try:
        count = int(getattr(cursor, "rowcount", -1) or -1)
    except Exception:  # noqa: BLE001
        count = -1
    if count > limit:
        return True
    return delivered >= limit and count != limit


def _cleanup(client: Any, cursor: Any) -> None:
    """Rollback + close: the READ ONLY transaction never survives a call."""
    for closer in (getattr(client, "rollback", None), getattr(cursor, "close", None)):
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - cleanup masks nothing
                pass


def _close_quietly(obj: Any) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
