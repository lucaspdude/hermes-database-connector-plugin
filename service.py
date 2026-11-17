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

import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from drivers import DriverMissingError, DriverRegistry, spec_for

PLUGIN_ID = "database-connector"
DB_FILENAME = "connector.db"

#: Accepted enums (spec ``connection-store`` -> "CRUD de conexões validado").
ENGINES = ("sqlserver", "postgres", "redis")
ENVIRONMENTS = ("prod", "homolog", "local")

QUERY_LOG_CAP = 1000
STATEMENT_MAX_CHARS = 500
ERROR_MAX_CHARS = 500

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


def connect(db_path: str | sqlite3.Connection) -> sqlite3.Connection:
    """Return a ready-to-use connection (accepts a path or an open one).

    Pragmas follow the design decision 2: WAL, ``foreign_keys=True`` (so
    ``ON DELETE CASCADE`` works) and ``busy_timeout`` so concurrent writers
    wait instead of failing.
    """
    conn = db_path if isinstance(db_path, sqlite3.Connection) else sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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
    conn = sqlite3.connect(db_path)
    migrate(conn)
    return conn


def init_store(ctx: Any) -> sqlite3.Connection:
    """Hermes wiring: open the plugin's SQLite store and migrate it.

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
    return migrate(conn)


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


def upsert_connection(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
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

    with conn:
        conn.execute(
            f"INSERT INTO connections ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            row,
        )
    return get_connection(conn, row["id"]) or row


def get_connection(conn: sqlite3.Connection, connection_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    return dict(row) if row else None


def list_connections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM connections ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def delete_connection(conn: sqlite3.Connection, connection_id: str) -> bool:
    """Delete a connection; ``query_log`` rows follow via ``ON DELETE CASCADE``."""
    with conn:
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
    conn: sqlite3.Connection,
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
    with conn:
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
    conn: sqlite3.Connection,
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
    with conn:
        cursor = conn.execute(
            "UPDATE query_log SET status = ?, rows = ?, duration_ms = ?, error = ? "
            "WHERE query_id = ?",
            (status, rows, duration_ms, message, query_id),
        )
    return cursor.rowcount > 0


def append_query_log(
    conn: sqlite3.Connection,
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
    with conn:
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
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    connection_id: str | None = None,
) -> list[dict[str, Any]]:
    """Newest first; optional filter by connection (read contract for /queries)."""
    sql = "SELECT * FROM query_log"
    params: list[Any] = []
    if connection_id:
        sql += " WHERE connection_id = ?"
        params.append(connection_id)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_query_log(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM query_log").fetchone()[0])


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


def _clean_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """One-line, secret-free rendering of an exception (never a stack trace)."""
    message = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
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
    """Best-effort server version, read from the driver without extra probes."""
    for holder in (getattr(client, "info", None), client):
        value = getattr(holder, "server_version", None) if holder is not None else None
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
    def probe() -> None:
        try:
            if spec.probe == "ping":
                client.ping()
                return
            cursor = client.cursor()
            try:
                cursor.execute(PROBE_STATEMENTS[engine])
                try:
                    cursor.fetchone()
                except Exception:  # noqa: BLE001 - some drivers have no row
                    pass
            finally:
                _close_quietly(cursor)
        finally:
            _close_quietly(client)

    _, exc = _run_with_deadline(statement_timeout_s, probe)
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
    version = _server_version(client)
    if version:
        payload["server_version"] = version
    return payload
