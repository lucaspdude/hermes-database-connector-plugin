"""Service layer (single source of truth) of the ``database-connector`` plugin.

Every function here takes a plain :class:`sqlite3.Connection` (or a database
path) so the store can be exercised without the Hermes runtime. The Hermes
wiring (``plugin_db``) lives only in :func:`init_store`, which imports the
Hermes private helper lazily — a module level import would break imports
outside the host process (and this repo's test suite).

Nothing in this module ever persists a secret **value**: only the env var
*name* (``secret_ref``) is written to SQLite. :func:`resolve_secret` fetches
the value in memory, at runtime, and reports only its state.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from drivers import DriverMissingError, DriverRegistry, spec_for

PLUGIN_ID = "database-connector"
DB_FILENAME = "connector.db"

#: Everything the store accepts: an open connection (tests, ``plugin_db``) or a
#: db path, from which each call opens its own connection (see :func:`write_tx`).
Store = sqlite3.Connection | str | os.PathLike[str]

#: Accepted enums (spec ``connection-store`` -> "CRUD de conexões validado").
ENGINES = ("sqlserver", "postgres", "redis")
ENVIRONMENTS = ("prod", "homolog", "local")

QUERY_LOG_CAP = 1000
STATEMENT_MAX_CHARS = 500
ERROR_MAX_CHARS = 500

#: ``esquema://usuario:senha@host[:porta][/banco]``: a driver echoes its DSN in
#: the error, and the DSN carries credentials — never logged, never answered.
_DSN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>)\],;]+")

#: Columns persisted for a connection (whitelist — see task 2.4).
CONNECTION_COLUMNS = (
    "id",
    "label",
    "engine",
    "environment",
    "host",
    "port",
    "database",
    "username",
    "secret_ref",
    "options_json",
    "read_only",
    "enabled",
    "created_at",
    "updated_at",
)

#: Keys that must never be accepted from a payload (values, not references).
_FORBIDDEN_KEYS = frozenset(
    {"password", "secret", "secret_value", "secret_value_plain", "dsn", "connection_string"}
)

_SCHEMA_VERSION = 1

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS connections (
      id           TEXT PRIMARY KEY,
      label        TEXT NOT NULL,
      engine       TEXT NOT NULL,
      environment  TEXT NOT NULL,
      host         TEXT NOT NULL,
      port         INTEGER,
      database     TEXT,
      username     TEXT,
      secret_ref   TEXT,
      options_json TEXT NOT NULL DEFAULT '{}',
      read_only    INTEGER NOT NULL DEFAULT 1,
      enabled      INTEGER NOT NULL DEFAULT 1,
      created_at   TEXT NOT NULL,
      updated_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS query_log (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      ts              TEXT NOT NULL,
      query_id        TEXT NOT NULL UNIQUE,
      connection_id   TEXT NOT NULL,
      engine          TEXT NOT NULL,
      environment     TEXT NOT NULL,
      statement       TEXT,
      rows            INTEGER,
      duration_ms     INTEGER,
      status          TEXT NOT NULL,
      error           TEXT,
      source          TEXT NOT NULL,
      session_id      TEXT,
      conversation_id TEXT,
      FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_query_log_ts ON query_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_query_log_conn ON query_log(connection_id, ts DESC)",
)

#: Secret states reported by :func:`resolve_secret`.
STATE_RESOLVED = "resolved"
STATE_MISSING = "missing"
STATE_UNSCOPED = "unscoped"


class ValidationError(ValueError):
    """Raised when a payload violates the connection contract."""


# --------------------------------------------------------------------------
# 2.1 store: connection, pragmas, versioned migration
# --------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Current instant as ISO-8601 UTC (``Z`` suffix, second resolution)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(db_path: str | os.PathLike[str] | sqlite3.Connection) -> sqlite3.Connection:
    """Return a ready-to-use connection (accepts a path or an open one).

    Pragmas follow the design decision 2: WAL, ``foreign_keys=True`` (so
    ``ON DELETE CASCADE`` works) and ``busy_timeout`` so concurrent writers
    wait instead of failing.
    """
    if isinstance(db_path, sqlite3.Connection):
        conn = db_path
    else:
        # ``check_same_thread=False``: an injected store may be handed to a
        # different thread than the one that opened it. Individual statements
        # still never overlap — see :func:`write_tx` / :func:`read_conn`.
        conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


#: Serialises every single touch of a **shared** store object: one
#: ``sqlite3.Connection`` must never be used by two threads at once (its
#: cursor/transaction state is shared, hence the old ``IndexError``/``locked``).
#: Wiring resolves the path once (see :func:`canonical_store`) so this lock is
#: belt-and-braces: it guards the rare call that still reaches the shared
#: connection and the in-memory fallback below.
_shared_store_lock = threading.RLock()


def database_path(conn: sqlite3.Connection) -> str | None:
    """Path of the ``main`` database of ``conn``; ``None`` when it is in-memory.

    Serialised on purpose: asking a **shared** connection for its path from
    several threads at once corrupts its cursor state (and every writer paid
    for it). The wiring never asks twice — see :func:`canonical_store`.
    """
    with _shared_store_lock:
        try:
            rows = conn.execute("PRAGMA database_list").fetchall()
        except Exception:  # noqa: BLE001 - closed/invalid connection: no path to reuse
            return None
    for row in rows:
        name, path = row[1], row[2]
        if name == "main":
            return str(path) if path else None
    return None


def canonical_store(store: Any) -> Store:
    """Canonical form of the store: **its db path** whenever it can be resolved.

    Called once, at wiring time (:func:`init_store` and
    ``plugin_api.set_store``): from then on every operation opens its own
    connection from the path and the shared connection is never touched again.
    Falls back to the connection itself only for a store that cannot be
    re-opened by path (``:memory:``), whose use stays serialised by
    :data:`_shared_store_lock`.
    """
    if isinstance(store, (str, os.PathLike)):
        return str(store)
    if isinstance(store, sqlite3.Connection):
        path = database_path(store)
        return path if path else store
    raise TypeError("store must be a sqlite3.Connection or a db path")


def _private_connection(store: Store) -> sqlite3.Connection | None:
    """A connection **of its own** for this call, or ``None`` when impossible.

    Root of the concurrent-write guarantee (spec ``connection-store`` ->
    "Escritas concorrentes"): re-opening the *same* db path gives every call a
    fresh connection, so no Python-level cursor/transaction state is ever
    shared between the REST threadpool threads — WAL + ``busy_timeout`` then do
    what they are actually for (making one writer wait for the other, across
    connections and processes). Returns ``None`` only for in-memory stores,
    which cannot be reached from another thread.
    """
    if isinstance(store, (str, os.PathLike)):
        return connect(str(store))
    # Fallback for a store that reached us still as a shared connection: the
    # read itself is serialised (:func:`database_path`), so parallel callers
    # never overlap inside the same ``sqlite3`` object.
    path = database_path(store)
    return connect(path) if path else None


@contextmanager
def write_tx(store: Store) -> Iterator[sqlite3.Connection]:
    """Yield a connection for **one short write transaction**, serialised. """
    private = _private_connection(store)
    if private is not None:
        try:
            migrate(private)  # idempotent: no-op once the schema is current
            with private:  # curta: BEGIN/COMMIT em volta de um unico statement
                yield private
        finally:
            private.close()
        return
    if not isinstance(store, sqlite3.Connection):  # pragma: no cover - wiring bug
        raise TypeError("store must be a sqlite3.Connection or a db path")
    with _shared_store_lock:
        with store:
            yield store


@contextmanager
def read_conn(store: Store) -> Iterator[sqlite3.Connection]:
    """Yield a connection for reads, private to this call when possible."""
    private = _private_connection(store)
    if private is not None:
        try:
            yield private
        finally:
            private.close()
        return
    with _shared_store_lock:
        yield store


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema up to :data:`SCHEMA_VERSION`, idempotently.

    Statements use ``IF NOT EXISTS`` and the version pragma is bumped once, so
    running this twice (two processes, or process restart) is a no-op.
    """
    conn = connect(conn)
    if schema_version(conn) < _SCHEMA_VERSION:
        with conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    return schema_version(conn)


def open_store(db_path: str) -> sqlite3.Connection:
    """Open ``db_path`` (or an in-memory/ existing connection) and migrate it."""
    conn = connect(db_path)
    migrate(conn)
    return conn


def init_store(ctx: Any) -> Store:
    """Hermes wiring: open the plugin's SQLite store, migrate it, return the
    canonical store (the db path) so no operation shares the connection.

    The import of ``plugin_db`` is lazy and inside the function: the helper
    only exists inside the Hermes process, so importing it at module level
    would break every consumer outside the host (tests included).
    """
    ctx_db = getattr(ctx, "plugin_db", None)
    if callable(ctx_db):
        conn = ctx_db()
    else:  # pragma: no cover - depends on the Hermes runtime being present
        from hermes_cli.plugins.plugin_storage import plugin_db  # type: ignore[import-not-found]

        conn = plugin_db(PLUGIN_ID, DB_FILENAME)
    migrate(conn)
    # Wiring resolves the path **once**: every operation from here on opens its
    # own connection from it, instead of asking the shared one.
    return canonical_store(conn)


# --------------------------------------------------------------------------
# 2.2 CRUD de conexões
# --------------------------------------------------------------------------


def _reject_forbidden_keys(payload: Mapping[str, Any]) -> None:
    offenders = sorted(key for key in payload if str(key).lower() in _FORBIDDEN_KEYS)
    if offenders:
        raise ValidationError(
            "refusing to store secret values; only the env var name 'secret_ref' is "
            f"accepted (offending keys: {', '.join(offenders)})"
        )


def _validate_choice(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(
            f"invalid {field}: {value!r}; expected one of {', '.join(allowed)}"
        )
    return value


def _options_json(payload: Mapping[str, Any]) -> str:
    options = payload.get("options_json", payload.get("options"))
    if options is None or options == "":
        return "{}"
    if isinstance(options, str):
        return options
    if isinstance(options, Mapping):
        return json.dumps(options, sort_keys=True)
    raise ValidationError("invalid options: expected an object or a JSON string")


def _row_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the payload, apply defaults and validate the enums."""
    if not isinstance(payload, Mapping):
        raise ValidationError("invalid connection: expected an object")

    connection_id = payload.get("id")
    if not isinstance(connection_id, str) or not connection_id.strip():
        raise ValidationError("invalid id: expected a non-empty slug")
    if not payload.get("host"):
        raise ValidationError("invalid host: expected a non-empty host")

    engine = _validate_choice(payload.get("engine"), ENGINES, "engine")
    environment = _validate_choice(payload.get("environment"), ENVIRONMENTS, "environment")

    now = utc_now_iso()
    return {
        "id": connection_id.strip(),
        "label": str(payload.get("label") or payload["id"]),
        "engine": engine,
        "environment": environment,
        "host": str(payload["host"]),
        "port": payload.get("port"),
        "database": payload.get("database"),
        "username": payload.get("username"),
        # secret_ref is the NAME of the env var — never its value.
        "secret_ref": payload.get("secret_ref"),
        "options_json": _options_json(payload),
        "read_only": 1 if payload.get("read_only", 1) else 0,
        "enabled": 1 if payload.get("enabled", 1) else 0,
        "created_at": now,
        "updated_at": now,
    }


def upsert_connection(store: Store, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Insert or update a connection by ``id`` (upsert).

    Validation happens before any write, so an invalid payload never leaves a
    row behind. ``created_at`` survives updates; ``updated_at`` is refreshed.
    """
    _reject_forbidden_keys(payload)
    row = _row_payload(payload)

    columns = ", ".join(CONNECTION_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in CONNECTION_COLUMNS)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in CONNECTION_COLUMNS
        if column not in ("id", "created_at")
    )

    with write_tx(store) as conn:
        conn.execute(
            f"INSERT INTO connections ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            row,
        )
    return get_connection(store, row["id"]) or row


def get_connection(store: Store, connection_id: str) -> dict[str, Any] | None:
    with read_conn(store) as conn:
        row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    return dict(row) if row else None


def list_connections(store: Store) -> list[dict[str, Any]]:
    with read_conn(store) as conn:
        rows = conn.execute("SELECT * FROM connections ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def delete_connection(store: Store, connection_id: str) -> bool:
    """Delete a connection; ``query_log`` rows follow via ``ON DELETE CASCADE``."""
    with write_tx(store) as conn:
        cursor = conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
    return cursor.rowcount > 0


# --------------------------------------------------------------------------
# 2.3 resolução de segredo (nunca persistida)
# --------------------------------------------------------------------------


def _is_unscoped_error(exc: BaseException) -> bool:
    """True when ``exc`` is the fail-closed ``UnscopedSecretError``.

    Matched by class when the host module is importable and by name otherwise,
    so a fake resolver in tests behaves like the real one.
    """
    try:  # pragma: no cover - only inside the Hermes process
        from agent.secret_scope import UnscopedSecretError  # type: ignore[import-not-found]

        if isinstance(exc, UnscopedSecretError):
            return True
    except Exception:  # noqa: BLE001 - module absent outside Hermes
        pass
    return type(exc).__name__ == "UnscopedSecretError"


def resolve_secret(
    connection_row: Mapping[str, Any],
    resolver: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Resolve a connection's secret **in memory** and report only its state.

    Returns ``{"secret_ref", "state", "value", "error"}`` where state is
    ``resolved`` (value obtained), ``missing`` (env absent/empty) or
    ``unscoped`` (multiplexing without a secret scope — no ``os.environ``
    fallback). The value is never persisted nor logged by this module.
    """
    secret_ref = connection_row.get("secret_ref") if connection_row else None
    if resolver is None:  # pragma: no cover - Hermes runtime only
        try:
            from agent.secret_scope import get_secret  # type: ignore[import-not-found]
        except ImportError:
            return {
                "secret_ref": secret_ref,
                "state": STATE_MISSING,
                "value": None,
                "error": "secret resolver unavailable (agent.secret_scope not importable)",
            }
        resolver = get_secret

    if not secret_ref:
        return {
            "secret_ref": secret_ref,
            "state": STATE_MISSING,
            "value": None,
            "error": "no secret_ref configured for this connection",
        }

    try:
        value = resolver(secret_ref)
    except Exception as exc:  # noqa: BLE001 - includes UnscopedSecretError
        if _is_unscoped_error(exc):
            return {
                "secret_ref": secret_ref,
                "state": STATE_UNSCOPED,
                "value": None,
                "error": f"secret scope unavailable for {secret_ref}: {exc}",
            }
        raise

    if value is None or (isinstance(value, str) and not value.strip()):
        return {
            "secret_ref": secret_ref,
            "state": STATE_MISSING,
            "value": None,
            "error": f"secret {secret_ref} is not set in the active profile",
        }
    return {"secret_ref": secret_ref, "state": STATE_RESOLVED, "value": value, "error": None}


# --------------------------------------------------------------------------
# 2.5 log de consultas: append do ciclo + poda com cap de 1.000 linhas
# --------------------------------------------------------------------------

#: Log sources (spec ``activity-log`` -> "Origem e sessão registradas").
SOURCE_AGENT = "agent"
SOURCE_UI = "ui"

#: One cycle per query — events are invalidation, the store is the truth
#: (spec ``activity-log``).
EVENT_QUERY_STARTED = "query.started"
EVENT_QUERY_FINISHED = "query.finished"
EVENT_QUERY_FAILED = "query.failed"
QUERY_EVENTS = (EVENT_QUERY_STARTED, EVENT_QUERY_FINISHED, EVENT_QUERY_FAILED)

#: Where the host may expose ``broadcast_plugin_event`` (same list as the REST
#: adapter: tried in order, imported lazily — none of them exists outside a
#: running Hermes, so importing at module load would break the plugin).
BROADCAST_SOURCES: tuple[tuple[str, str], ...] = (
    ("hermes.gateway.events", "broadcast_plugin_event"),
    ("hermes.gateway", "broadcast_plugin_event"),
    ("hermes.events", "broadcast_plugin_event"),
    ("gateway.events", "broadcast_plugin_event"),
)

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


def sanitize_statement(statement: str | None, limit: int = STATEMENT_MAX_CHARS) -> str | None:
    """Redact literals and truncate a statement before it is logged."""
    if statement is None:
        return None
    text = _COMMENT_RE.sub(" ", statement)
    text = _LINE_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub("?", text)
    text = _NUMBER_RE.sub("?", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _prune_query_log(conn: sqlite3.Connection, cap: int = QUERY_LOG_CAP) -> int:
    """Delete rows beyond ``cap``, keeping the newest ones (same transaction)."""
    cursor = conn.execute(
        "DELETE FROM query_log WHERE id NOT IN "
        f"(SELECT id FROM query_log ORDER BY ts DESC, id DESC LIMIT {int(cap)})"
    )
    return cursor.rowcount or 0


def new_query_id() -> str:
    return str(uuid.uuid4())


def start_query_log(
    store: Store,
    *,
    connection_id: str,
    engine: str,
    environment: str,
    statement: str | None = None,
    source: str,
    query_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    ts: str | None = None,
) -> str:
    """Insert a ``running`` row (query.started) and prune the log."""
    query_id = query_id or new_query_id()
    with write_tx(store) as conn:
        conn.execute(
            "INSERT INTO query_log (ts, query_id, connection_id, engine, environment, "
            "statement, rows, duration_ms, status, error, source, session_id, "
            "conversation_id) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'running', NULL, "
            "?, ?, ?)",
            (
                ts or utc_now_iso(),
                query_id,
                connection_id,
                engine,
                environment,
                sanitize_statement(statement),
                source,
                session_id,
                conversation_id,
            ),
        )
        _prune_query_log(conn)
    return query_id


def finish_query_log(
    store: Store,
    query_id: str,
    *,
    status: str = "ok",
    rows: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> bool:
    """Update the started row to ``ok``/``error`` (query.finished | query.failed).

    One line per cycle: the started row is updated, never duplicated.
    """
    message = None
    if error is not None:
        message = str(error)[:ERROR_MAX_CHARS]
    with write_tx(store) as conn:
        cursor = conn.execute(
            "UPDATE query_log SET status = ?, rows = ?, duration_ms = ?, error = ? "
            "WHERE query_id = ?",
            (status, rows, duration_ms, message, query_id),
        )
    return cursor.rowcount > 0


def append_query_log(
    store: Store,
    *,
    connection_id: str,
    engine: str,
    environment: str,
    statement: str | None = None,
    source: str,
    status: str = "ok",
    rows: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    query_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    ts: str | None = None,
) -> str:
    """Append a final log row in one write, pruning beyond the cap with it."""
    query_id = query_id or new_query_id()
    message = str(error)[:ERROR_MAX_CHARS] if error is not None else None
    with write_tx(store) as conn:
        conn.execute(
            "INSERT INTO query_log (ts, query_id, connection_id, engine, environment, "
            "statement, rows, duration_ms, status, error, source, session_id, "
            "conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts or utc_now_iso(),
                query_id,
                connection_id,
                engine,
                environment,
                sanitize_statement(statement),
                rows,
                duration_ms,
                status,
                message,
                source,
                session_id,
                conversation_id,
            ),
        )
        _prune_query_log(conn)
    return query_id


def list_query_log(
    store: Store,
    *,
    limit: int = 100,
    connection_id: str | None = None,
    since_id: int | None = None,
) -> list[dict[str, Any]]:
    """Newest first; optional filters by connection and by ``id > since_id``.

    ``since_id`` is the cursor the UI uses to reconcile lost events: only rows
    newer than the last one it saw are returned.
    """
    sql = "SELECT * FROM query_log"
    params: list[Any] = []
    clauses: list[str] = []
    if connection_id:
        clauses.append("connection_id = ?")
        params.append(connection_id)
    if since_id is not None:
        clauses.append("id > ?")
        params.append(int(since_id))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with read_conn(store) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_query_log(store: Store) -> int:
    with read_conn(store) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM query_log").fetchone()[0])


def last_query_log_id(store: Store) -> int:
    """Highest ``query_log.id`` — the sequence used for reconciliation."""
    with read_conn(store) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM query_log").fetchone()
    return int(row[0]) if row else 0


def list_active_queries(
    store: Store,
    *,
    session_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Rows still ``running`` (newest first), optionally narrowed to a session."""
    sql = "SELECT * FROM query_log WHERE status = 'running'"
    params: list[Any] = []
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with read_conn(store) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_log_counters(store: Store) -> dict[str, int]:
    """Row counts per status — the ``counters`` block of ``GET /status``."""
    counters = {"running": 0, "ok": 0, "error": 0}
    with read_conn(store) as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM query_log GROUP BY status").fetchall()
    for row in rows:
        key = str(row[0]) if row[0] in counters else None
        if key:
            counters[key] = int(row[1])
    counters["total"] = sum(counters[key] for key in ("running", "ok", "error"))
    return counters


def count_connections(store: Store) -> int:
    with read_conn(store) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0])


def status_snapshot(
    store: Store,
    *,
    session_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """``{ active, connections, last_seq, counters }`` (spec backend-api).

    Everything is read from the store, so a UI that lost an event converges by
    re-reading this endpoint (it never trusts the event payload).
    """
    return {
        "active": list_active_queries(store, session_id=session_id, limit=limit),
        "connections": count_connections(store),
        "last_seq": last_query_log_id(store),
        "counters": query_log_counters(store),
    }


def connection_view(
    connection_row: Mapping[str, Any],
    resolver: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Public view of a connection row: adds ``secret_state``, never the value.

    The row itself only ever carries ``secret_ref``; the state is resolved in
    memory here. A resolver failure never leaks a traceback — it is reported as
    ``missing`` with a clean error message.
    """
    view = dict(connection_row)
    try:
        secret = resolve_secret(connection_row, resolver=resolver)
    except Exception as exc:  # noqa: BLE001 - HTTP layer must see a clean error
        secret = {
            "state": STATE_MISSING,
            "error": _clean_error(exc),
        }
    view["secret_state"] = secret["state"]
    view.pop("secret_value", None)
    # defensive: drop anything that could ever hold a resolved value
    for key in ("password", "secret", "secret_value"):
        view.pop(key, None)
    return view


# --------------------------------------------------------------------------
# 2.6 ciclo único de consulta (design 2): log "running -> ok/error" + eventos.
#     Eventos reais via broadcast pendentes (hostgator): a emissão usa o
#     emissor injetado e cai no ``broadcast_plugin_event`` do host.
# --------------------------------------------------------------------------

#: Defaults of the read policy (spec ``query-policy`` -> "Timeouts e limites").
DEFAULT_MAX_ROWS = 200
MAX_ROWS_CAP = 1000

#: Stable error codes answered to the agent (task 2.2).
CODE_TIMEOUT = "timeout"
CODE_DB_ERROR = "db_error"
CODE_DRIVER_MISSING = "driver_missing"
CODE_SECRET_MISSING = "secret_missing"
CODE_NOT_FOUND = "not_found"
CODE_READ_ONLY = "read_only"
ERROR_CODES = (
    CODE_TIMEOUT,
    CODE_DB_ERROR,
    CODE_DRIVER_MISSING,
    CODE_SECRET_MISSING,
    CODE_NOT_FOUND,
    CODE_READ_ONLY,
)

#: Toggle declared in ``plugin.yaml`` -> ``config_schema`` (boolean, default
#: true) and read with the injected ``ctx.get_config("log_statement", true)``.
CONFIG_LOG_STATEMENT = "log_statement"

_query_event_emitter: Callable[..., Any] | None = None
_config_getter: Callable[[str, Any], Any] | None = None


def set_query_event_emitter(emitter: Callable[..., Any] | None) -> None:
    """Inject the emitter of ``query.*`` (signature of the host:
    ``emitter(plugin_id, event, payload)``)."""
    global _query_event_emitter
    _query_event_emitter = emitter


def get_query_event_emitter() -> Callable[..., Any] | None:
    return _query_event_emitter


def set_config_getter(getter: Callable[[str, Any], Any] | None) -> None:
    """Inject the config getter (``ctx.get_config`` is plugged by ``register``)."""
    global _config_getter
    _config_getter = getter


def get_config(key: str, default: Any = None) -> Any:
    """Read a plugin setting: injected getter first, then ``default``.

    Outside the host (and in the tests) there is no getter, so the documented
    default wins — ``log_statement`` is therefore *on* by default.
    """
    getter = _config_getter
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:  # noqa: BLE001 - jamais derrubar o ciclo por config
            return default
    return default


def resolve_broadcast() -> Callable[..., Any] | None:
    """Return the host's ``broadcast_plugin_event``, or ``None`` outside Hermes."""
    for module_name, attribute in BROADCAST_SOURCES:
        try:
            module = __import__(module_name, fromlist=[attribute])
        except Exception:  # noqa: BLE001 - module simply not present here
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate
    return None


def emit_query_event(
    event: str,
    payload: Mapping[str, Any],
    emitter: Callable[..., Any] | None = None,
) -> bool:
    """Emit one ``query.*`` event. **Never** raises, never decides the result.

    Injected emitter first (tests/listener), then the host's real
    ``broadcast_plugin_event`` resolved lazily. A dead bus must not lose a
    query: the store already holds the line and ``GET /status`` reconciles.
    """
    try:
        target = emitter or get_query_event_emitter() or resolve_broadcast()
        if target is None:
            return False
        target(PLUGIN_ID, event, dict(payload))
        return True
    except Exception:  # noqa: BLE001 - emissao falha nao derruba o ciclo
        return False


def classify_driver_error(exc: BaseException) -> str:
    """Map any driver/engine failure to one stable ``code`` (task 2.2).

    Single mapping, used by the cycle *and* by the tools of the next change, so
    the agent sees the same vocabulary everywhere.
    """
    if isinstance(exc, DriverMissingError):
        return CODE_DRIVER_MISSING
    declared = getattr(exc, "code", None)
    if declared in ERROR_CODES:
        return str(declared)
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        # driver-specific timeouts (redis/psycopg/FreeTDS) are subclasses with
        # "Timeout" in the name; our own deadline raises TimeoutError.
        return CODE_TIMEOUT
    return CODE_DB_ERROR


def clean_driver_error(
    exc: BaseException,
    *,
    secrets: Iterable[str] = (),
    hosts: Iterable[str] = (),
) -> str:
    """Truncated (500), DSN-less, secret-less rendering of a driver failure.

    Reuses :func:`_clean_error`/:func:`_scrub_dsn`: the whole DSN goes away (it
    carries credentials *and* topology) and any resolved secret/host that the
    driver echoed back is masked as well.
    """
    text = _clean_error(exc, secrets)
    for host in hosts:
        if host:
            text = text.replace(str(host), "[host omitido]")
    return text[:ERROR_MAX_CHARS]


def map_driver_error(
    exc: BaseException,
    *,
    secrets: Iterable[str] = (),
    hosts: Iterable[str] = (),
) -> tuple[str, str]:
    """``(code, message)`` for a failure: mapping + redaction in one call."""
    return classify_driver_error(exc), clean_driver_error(exc, secrets=secrets, hosts=hosts)


def _driver_module(
    engine: str,
    registry: DriverRegistry,
    injected: Any = None,
) -> Any:
    """Uniform driver **adapter** of ``engine`` (``connect``/``execute``/...).

    ``registry.load`` stays the single lazy import point of the *library*
    (design 1) and its answer is handed to the adapter (so the tests inject a
    fake library and still exercise the real READ ONLY behaviour). ``injected``
    short-circuits everything when a whole adapter is provided.
    """
    if injected is not None:
        return injected
    module = importlib.import_module(f"drivers.{engine}")
    library = registry.load(engine)  # raises DriverMissingError when absent
    setter = getattr(module, "set_driver_module", None)
    if callable(setter):
        try:
            setter(library)
        except TypeError:  # noqa: PERF203 - adapters with extra arguments
            setter(library, optional=None)
    return module


def _log_statement_enabled(getter: Callable[[str, Any], Any] | None = None) -> bool:
    """The ``log_statement`` toggle: injected getter, provider, then ``True``."""
    if callable(getter):
        try:
            return bool(getter(CONFIG_LOG_STATEMENT, True))
        except Exception:  # noqa: BLE001 - config nunca derruba o ciclo
            return True
    return bool(get_config(CONFIG_LOG_STATEMENT, True))


def _positive_int(value: Any, default: int) -> int:
    """Positive int or ``default`` (``max_rows`` and friends are limits)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _cycle_failure(
    code: str,
    error: str,
    *,
    started: float,
    duration_ms: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "ok": False,
        "code": code,
        "error": error[:ERROR_MAX_CHARS],
        "status": "error",
        "query_id": None,
        "duration_ms": (
            duration_ms
            if duration_ms is not None
            else int(round((time.perf_counter() - started) * 1000))
        ),
    }
    payload.update(extra)
    return payload


def _execute_on(
    driver_module: Any,
    client: Any,
    spec: Any,
    *,
    statement: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    args: Sequence[Any] | None = None,
    statement_timeout_s: float | None = None,
) -> dict[str, Any]:
    """One execution through the uniform interface (SQL: statement; Redis:
    command + args). Extracted from the cycle so the deadline wraps it."""
    if spec.probe == "ping":
        return driver_module.execute(
            client,
            statement or "",
            tuple(args or ()),
            statement_timeout_s=statement_timeout_s,
        )
    if not statement:
        raise ValueError(f"statement ausente para a engine '{spec.engine}'")
    return driver_module.execute(
        client,
        statement,
        _positive_int(max_rows, DEFAULT_MAX_ROWS),
        statement_timeout_s=statement_timeout_s,
    )


def run_query(
    store: Store,
    connection_row: Mapping[str, Any] | None,
    statement: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    source: str = SOURCE_AGENT,
    session_id: str | None = None,
    *,
    args: Sequence[Any] | None = None,
    conversation_id: str | None = None,
    registry: DriverRegistry | None = None,
    resolver: Callable[[str], Any] | None = None,
    emitter: Callable[..., Any] | None = None,
    config_getter: Callable[[str, Any], Any] | None = None,
    driver: Any | None = None,
    query_id: str | None = None,
) -> dict[str, Any]:
    """Run **one** query end to end: log row + ``query.*`` events + result.

    Design decision 2: one single cycle for every engine — SQL carries the
    statement, Redis carries the command in ``statement`` and its arguments in
    ``args``. The order is fixed and it is what guarantees there is never an
    orphan "running" row:

    1. pre-flight (unknown connection, engine, driver, secret) — nothing is
       logged and no event is emitted, so a failure here leaves no open cycle;
    2. the ``running`` line (:func:`start_query_log`) + ``query.started``;
    3. the execution, bounded by the connection's own timeouts;
    4. the **same** line updated to ``ok``/``error`` (:func:`finish_query_log`)
       + exactly one of ``query.finished``/``query.failed``.

    Every write to the log is redacted here and only here (task 2.3): the
    statement is truncated at 500 chars with its literals masked and without
    comments, the error comes without DSN/secret/host, and neither ever carries
    the result nor the connection password. ``args`` and the result are never
    logged at all.

    Returns ``{ok, query_id, rows|value, row_count, truncated, duration_ms}``
    or ``{ok: False, code, error, duration_ms}`` with a stable ``code``.
    """
    started = time.perf_counter()
    row = dict(connection_row) if isinstance(connection_row, Mapping) else {}
    connection_id = str(row.get("id")) if row.get("id") else None
    engine = row.get("engine")
    environment = row.get("environment")
    display_database = row.get("database")

    if not connection_id:
        return _cycle_failure(
            CODE_NOT_FOUND,
            "conexao nao informada: o ciclo exige uma conexao com id",
            started=started,
            connection_id=connection_id,
            engine=engine,
        )

    try:
        spec = spec_for(engine)
    except (ValueError, TypeError) as exc:
        # Unknown engine: reported as a db failure to keep the vocabulary closed.
        return _cycle_failure(
            CODE_DB_ERROR, clean_driver_error(exc), started=started,
            connection_id=connection_id, engine=engine,
        )

    registry = registry or DriverRegistry()

    try:
        driver_module = _driver_module(spec.engine, registry, driver)
    except DriverMissingError as exc:
        return _cycle_failure(
            CODE_DRIVER_MISSING,
            f"driver '{spec.module}' nao instalado para a engine '{engine}'; "
            f"instale com: {exc.install}",
            started=started,
            connection_id=connection_id,
            engine=engine,
            module=spec.module,
            install=exc.install,
        )

    # Segredo em memoria: resolvido ANTES do started (nunca ha linha orfa de
    # um ciclo que nao pode nem abrir a conexao).
    password: str | None = None
    if row.get("secret_ref"):
        try:
            secret = resolve_secret(row, resolver=resolver)
        except Exception as exc:  # noqa: BLE001 - resolver errors stay clean
            return _cycle_failure(
                CODE_SECRET_MISSING,
                clean_driver_error(exc),
                started=started,
                connection_id=connection_id,
                engine=engine,
            )
        if secret["state"] != STATE_RESOLVED:
            return _cycle_failure(
                CODE_SECRET_MISSING,
                str(secret.get("error") or "segredo indisponivel"),
                started=started,
                connection_id=connection_id,
                engine=engine,
                secret_state=secret["state"],
            )
        password = secret["value"]

    connect_timeout_s, statement_timeout_s = connection_timeouts(row)

    query_id = start_query_log(
        store,
        connection_id=connection_id,
        engine=str(engine),
        environment=str(environment),
        statement=statement if _log_statement_enabled(config_getter) else None,
        source=source,
        query_id=query_id,
        session_id=session_id,
        conversation_id=conversation_id,
    )
    started_at = utc_now_iso()

    base_payload: dict[str, Any] = {
        "query_id": query_id,
        "connection_id": connection_id,
        "label": row.get("label") or connection_id,
        "engine": engine,
        "environment": environment,
        # Nome de exibicao do banco — nunca o DSN.
        "database": display_database,
        "session_id": session_id,
        "started_at": started_at,
    }
    started_payload = dict(base_payload)
    started_payload["status"] = "running"
    emit_query_event(EVENT_QUERY_STARTED, started_payload, emitter)

    try:
        client, exc = _run_with_deadline(
            connect_timeout_s, lambda: driver_module.connect(row, password)
        )
        if exc is not None:
            raise exc
        try:
            result, exc = _run_with_deadline(
                statement_timeout_s,
                lambda: _execute_on(
                    driver_module,
                    client,
                    spec,
                    statement=statement,
                    max_rows=min(_positive_int(max_rows, DEFAULT_MAX_ROWS), MAX_ROWS_CAP),
                    args=args,
                    statement_timeout_s=statement_timeout_s,
                ),
            )
            if exc is not None:
                raise exc
        finally:
            _close_quietly(client)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        code, message = map_driver_error(
            exc, secrets=[password] if password else (), hosts=[row.get("host")]
        )
        duration_ms = int(round((time.perf_counter() - started) * 1000))
        finish_query_log(
            store, query_id, status="error", duration_ms=duration_ms, error=message
        )
        failure_payload = dict(base_payload)
        failure_payload.update(
            {
                "status": "error",
                "code": code,
                "error": message,
                "duration_ms": duration_ms,
                "finished_at": utc_now_iso(),
                "rows": None,
            }
        )
        emit_query_event(EVENT_QUERY_FAILED, failure_payload, emitter)
        return {
            "ok": False,
            "code": code,
            "error": message,
            "status": "error",
            "query_id": query_id,
            "connection_id": connection_id,
            "engine": engine,
            "duration_ms": duration_ms,
        }

    duration_ms = int(result.get("duration_ms") or 0) if isinstance(result, Mapping) else 0
    rows = result.get("rows") if isinstance(result, Mapping) else None
    value = result.get("value") if isinstance(result, Mapping) else None
    row_count = result.get("row_count") if isinstance(result, Mapping) else None
    if row_count is None and isinstance(rows, list):
        row_count = len(rows)
    truncated = bool(result.get("truncated")) if isinstance(result, Mapping) else False

    finish_query_log(
        store, query_id, status="ok", rows=row_count, duration_ms=duration_ms
    )
    finished_payload = dict(base_payload)
    finished_payload.update(
        {
            "status": "ok",
            "rows": row_count,
            "duration_ms": duration_ms,
            "truncated": truncated,
            "finished_at": utc_now_iso(),
        }
    )
    emit_query_event(EVENT_QUERY_FINISHED, finished_payload, emitter)

    return {
        "ok": True,
        "status": "ok",
        "query_id": query_id,
        "connection_id": connection_id,
        "engine": engine,
        "environment": environment,
        "rows": rows,
        "value": value,
        "row_count": row_count,
        "truncated": truncated,
        "duration_ms": duration_ms,
        "started_at": started_at,
        "finished_at": finished_payload["finished_at"],
    }


# --------------------------------------------------------------------------
# 3.1 teste de conexão — probe real; validação contra instância real pendente
#     (hostgator: SQL Server e Redis externos, Postgres local/Docker)
# --------------------------------------------------------------------------

#: Only used when the connection's ``options`` do not override them.
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_STATEMENT_TIMEOUT_S = 30

#: Probe mínimo por engine (spec backend-api -> "Teste de conexao").
PROBE_STATEMENTS = {"sqlserver": "SELECT 1", "postgres": "SELECT 1"}


def connection_options(connection_row: Mapping[str, Any]) -> dict[str, Any]:
    """Decode the connection's options (``options_json`` *or* ``options``).

    Accepted as a JSON string or as an object, mirroring what
    :func:`upsert_connection` normalises into the row.
    """
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


def _timeout_value(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def connection_timeouts(connection_row: Mapping[str, Any]) -> tuple[float, float]:
    """``(connect_timeout_s, statement_timeout_s)`` for a connection row."""
    options = connection_options(connection_row)
    return (
        _timeout_value(options.get("connect_timeout_s"), DEFAULT_CONNECT_TIMEOUT_S),
        _timeout_value(options.get("statement_timeout_s"), DEFAULT_STATEMENT_TIMEOUT_S),
    )


def _failure(code: str, error: str, *, started: float, **extra: Any) -> dict[str, Any]:
    """Uniform failure payload: clear message, no traceback, no secret/DSN."""
    payload = {
        "ok": False,
        "code": code,
        "latency_ms": int(round((time.perf_counter() - started) * 1000)),
        "error": error,
    }
    payload.update(extra)
    return payload


def _scrub_dsn(text: str) -> str:
    """Remove ``esquema://usuario:senha@host/banco`` from a driver message.

    The whole DSN goes away (spec backend-api: "nunca o DSN completo"): it
    carries credentials *and* the topology of the environment, so keeping even
    ``esquema://`` around would still be leaking.
    """
    return _DSN_RE.sub("[dsn omitido]", text)


def _clean_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """One-line, secret-free rendering of an exception (never a stack trace)."""
    message = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    text = _scrub_dsn(text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "***")
    # A driver message may echo the DSN; keep only the first line, truncated.
    return text.splitlines()[0][:ERROR_MAX_CHARS]


def _run_with_deadline(seconds: float, func: Callable[[], Any]) -> tuple[Any, BaseException | None]:
    """Run ``func`` in a daemon thread, returning ``(value, error | timeout)``.

    The budget is enforced even when the driver itself ignores its own timeout
    kwargs. The thread is a daemon: a stuck socket never blocks interpreter
    exit (and never leaks a traceback into the caller).
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - reported, never raised
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if "value" in outcome:
        return outcome["value"], None
    if "error" in outcome:
        return None, outcome["error"]
    return None, TimeoutError(f"operacao excedeu o timeout de {seconds:g}s")


def _close_quietly(obj: Any) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - closing must never mask the result
            pass


def _server_version(client: Any) -> str | None:
    """Best-effort server version, read from the driver without extra probes.

    Every attribute read is protected: a driver may raise for a client it
    considers closed (psycopg raises ``OperationalError`` from ``.info``), and
    that must never turn a successful probe into a 500.
    """
    for holder_name in ("info", None):
        try:
            holder = client if holder_name is None else getattr(client, holder_name)
            value = getattr(holder, "server_version", None) if holder is not None else None
        except Exception:  # noqa: BLE001 - closed/blocked client: report nothing
            continue
        if isinstance(value, (str, int, float)) and value:
            return str(value)
    return None


def _open_sql(driver: Any, engine: str, row: Mapping[str, Any], password: str | None,
              connect_timeout_s: float, statement_timeout_s: float) -> Any:
    host = str(row.get("host") or "")
    port = row.get("port")
    database = row.get("database")
    username = row.get("username")

    if engine == "postgres":  # psycopg
        kwargs: dict[str, Any] = {"host": host, "connect_timeout": max(int(connect_timeout_s), 1)}
        if port:
            kwargs["port"] = int(port)
        if database:
            kwargs["dbname"] = database
        if username:
            kwargs["user"] = username
        if password is not None:
            kwargs["password"] = password
        return driver.connect(**kwargs)

    # sqlserver -> pymssql (FreeTDS: "host:port")
    kwargs = {
        "server": f"{host}:{port}" if port else host,
        "login_timeout": max(int(connect_timeout_s), 1),
        "timeout": max(int(statement_timeout_s), 1),
    }
    if username:
        kwargs["user"] = username
    if password is not None:
        kwargs["password"] = password
    if database:
        kwargs["database"] = database
    return driver.connect(**kwargs)


def _open_redis(driver: Any, row: Mapping[str, Any], password: str | None,
                connect_timeout_s: float, statement_timeout_s: float) -> Any:
    kwargs: dict[str, Any] = {
        "host": str(row.get("host") or ""),
        "port": int(row.get("port") or 6379),
        "socket_connect_timeout": connect_timeout_s,
        "socket_timeout": statement_timeout_s,
    }
    if row.get("username"):
        kwargs["username"] = row["username"]
    if password is not None:
        kwargs["password"] = password
    if row.get("database"):
        try:
            kwargs["db"] = int(row["database"])
        except (TypeError, ValueError):
            pass
    factory = getattr(driver, "Redis", None)
    return (factory if callable(factory) else driver)(**kwargs)


def test_connection(connection: Mapping[str, Any], registry: DriverRegistry | None = None,
                    resolver: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Open a real connection for the engine, run the minimal probe, report.

    Returns ``{"ok": True, "latency_ms": int, "server_version": str?}`` or
    ``{"ok": False, "code": ..., "error": "..."}`` — always a clean message:
    no stack trace, no password and no full DSN leak into the payload.

    ``registry`` accepts an injected driver (fakes/tests: real ``drivers/``
    modules land in the next change); ``resolver`` resolves the secret.
    Timeouts come from the connection's ``options``
    (``connect_timeout_s``/``statement_timeout_s``, defaults 10s/30s).
    """
    started = time.perf_counter()
    row = dict(connection) if isinstance(connection, Mapping) else {}
    engine = row.get("engine")

    try:
        spec = spec_for(engine)
    except ValueError as exc:
        return _failure("invalid_engine", str(exc), started=started)

    registry = registry or DriverRegistry()

    try:
        driver = registry.load(engine)
    except DriverMissingError as exc:
        return _failure(
            "driver_missing",
            f"driver '{spec.module}' nao instalado para a engine '{engine}'; "
            f"instale com: {exc.install}",
            started=started,
            engine=engine,
            module=spec.module,
            install=exc.install,
        )

    # Segredo resolvido em memoria — nunca persistido, nunca no payload.
    password: str | None = None
    if row.get("secret_ref"):
        try:
            secret = resolve_secret(row, resolver=resolver)
        except Exception as exc:  # noqa: BLE001 - resolver errors stay clean too
            return _failure("secret_error", _clean_error(exc),
                            started=started, engine=engine)
        if secret["state"] != STATE_RESOLVED:
            return _failure(
                f"secret_{secret['state']}",
                str(secret.get("error") or "segredo indisponivel"),
                started=started,
                engine=engine,
                secret_state=secret["state"],
            )
        password = secret["value"]
        secrets_to_scrub = [password]
    else:
        secrets_to_scrub = []

    connect_timeout_s, statement_timeout_s = connection_timeouts(row)

    # Fase 1: abrir (budget = connect_timeout_s)
    if spec.probe == "ping":
        client, exc = _run_with_deadline(
            connect_timeout_s,
            lambda: _open_redis(driver, row, password, connect_timeout_s, statement_timeout_s),
        )
    else:
        client, exc = _run_with_deadline(
            connect_timeout_s,
            lambda: _open_sql(driver, engine, row, password, connect_timeout_s,
                              statement_timeout_s),
        )
    if exc is not None:
        _close_quietly(client)
        if isinstance(exc, TimeoutError):
            return _failure(
                "connect_timeout",
                f"timeout de {connect_timeout_s:g}s excedido ao conectar em "
                f"{row.get('host')} (engine '{engine}')",
                started=started,
                engine=engine,
            )
        return _failure("connect_error", _clean_error(exc, secrets_to_scrub),
                        started=started, engine=engine)

    # Fase 2: sonda mínima (budget = statement_timeout_s)
    def probe() -> dict[str, Any]:
        """Probe and collect everything useful **while the client is open**.

        The version is read here, never after ``close``: several drivers
        (psycopg among them) raise once the client is closed, and that failure
        used to turn a successful test into a 500 — before the log line was
        even written.
        """
        outcome: dict[str, Any] = {}
        try:
            if spec.probe == "ping":
                client.ping()
            else:
                cursor = client.cursor()
                try:
                    cursor.execute(PROBE_STATEMENTS[engine])
                    try:
                        cursor.fetchone()
                    except Exception:  # noqa: BLE001 - some drivers have no row
                        pass
                finally:
                    _close_quietly(cursor)
            version = _server_version(client)  # ainda aberto (e tolerante a falha)
            if version:
                outcome["server_version"] = version
        finally:
            # fechar nunca invalida o resultado ja capturado acima
            _close_quietly(client)
        return outcome

    info, exc = _run_with_deadline(statement_timeout_s, probe)
    if exc is not None:
        _close_quietly(client)
        if isinstance(exc, TimeoutError):
            return _failure(
                "statement_timeout",
                f"timeout de {statement_timeout_s:g}s excedido na sonda da engine "
                f"'{engine}'",
                started=started,
                engine=engine,
            )
        return _failure("probe_error", _clean_error(exc, secrets_to_scrub),
                        started=started, engine=engine)

    payload: dict[str, Any] = {
        "ok": True,
        "latency_ms": int(round((time.perf_counter() - started) * 1000)),
    }
    if isinstance(info, Mapping) and info.get("server_version"):
        payload["server_version"] = str(info["server_version"])
    return payload
