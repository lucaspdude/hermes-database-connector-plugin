"""HTTP adapter for the database-connector plugin (dashboard half).

Design decision 1 (``add-plugin-backend``): this module is a **thin** adapter —
every route only translates HTTP into a call to :mod:`service`, which owns the
store and all the rules. Nothing here re-implements validation, timeouts or the
secret policy; duplicating them would create a second source of truth shared
with the future agent tools.

The store is injected (``set_store``) so tests can plug a throwaway SQLite file
in ``tmp_path`` and the Hermes host can plug ``plugin_db`` from ``register``.
No route ever learns how the store was opened.

Responses never carry a secret **value** (spec backend-api): only ``secret_ref``
plus the resolved ``secret_state`` (``resolved``/``missing``/``unscoped``).
Errors are always ``{ error, detail }`` in plain Portuguese, without stack
trace, DSN or secret.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

import drivers
import service

PLUGIN_ID = "database-connector"

#: Log source of everything triggered by the REST layer (spec backend-api).
SOURCE_UI = "ui"

router = APIRouter()


# --------------------------------------------------------------------------
# providers — what ``register(ctx)`` plugs in later (wiring do host Hermes)
# --------------------------------------------------------------------------

_store: sqlite3.Connection | None = None
_store_factory: Callable[[], Any] | None = None
_driver_registry: drivers.DriverRegistry | None = None
_secret_resolver: Callable[[str], Any] | None = None


def set_store(store: Any | None) -> None:
    """Plug the store: an open ``sqlite3.Connection`` **or** a zero-arg factory.

    ``register(ctx)`` calls :func:`configure` (or this) with the connection
    returned by ``plugin_db``; tests call it with a ``tmp_path`` database.
    """
    global _store, _store_factory
    _store = None
    _store_factory = None
    if store is None:
        return
    if isinstance(store, sqlite3.Connection):
        _store = store
    elif callable(store):
        _store_factory = store
    else:  # pragma: no cover - defensive: anything else is a wiring bug
        raise TypeError("store must be a sqlite3.Connection or a zero-arg callable")


def get_store() -> sqlite3.Connection:
    """Return the configured store, or fail cleanly when not wired yet.

    The failure is explicit on purpose: replying with an empty payload would
    look like "no connections" to the UI (the silent-empty trap, R5).
    """
    global _store
    if _store_factory is not None:
        _store = service.connect(_store_factory())
    if _store is None:
        raise _HttpError(
            503,
            "store_unavailable",
            "store do plugin nao configurado: habilite o plugin e reinicie o gateway",
        )
    return service.connect(_store)


def configure(ctx: Any) -> sqlite3.Connection:
    """Wiring for the Hermes host: open the plugin store and remember it."""
    conn = service.init_store(ctx)
    set_store(conn)
    return conn


def set_driver_registry(registry: drivers.DriverRegistry | None) -> None:
    """Inject the driver registry (tests use fakes; the host uses detection)."""
    global _driver_registry
    _driver_registry = registry


def get_driver_registry() -> drivers.DriverRegistry:
    return _driver_registry if _driver_registry is not None else drivers.default_registry()


def set_secret_resolver(resolver: Callable[[str], Any] | None) -> None:
    """Inject the secret resolver (``agent.secret_scope.get_secret`` in-host)."""
    global _secret_resolver
    _secret_resolver = resolver


def get_secret_resolver() -> Callable[[str], Any] | None:
    return _secret_resolver


def _notify_connection_changed(action: str, connection_id: str) -> None:
    """Stub for the ``connection.changed`` signal (task 4.6, out of this slice).

    Task 4.6 replaces this body with the real ``broadcast_plugin_event`` call;
    every CRUD handler already goes through the hook, so that change touches
    one function only. Deliberately a no-op for now — no event is emitted.
    """
    return None


# --------------------------------------------------------------------------
# erros: { error, detail } — sem stack trace, DSN ou segredo (spec backend-api)
# --------------------------------------------------------------------------


class _HttpError(Exception):
    """Internal carrier: every failure leaves the module as ``{error, detail}``."""

    def __init__(self, status_code: int, code: str, detail: str):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code, "detail": detail})


def _clean(exc: BaseException) -> str:
    """One-line rendering reused from the service (never a stack trace)."""
    return service._clean_error(exc)  # noqa: SLF001 - same package


def _run(operation: Callable[[], Any]) -> Any:
    """Execute a route body, converting every failure to ``{error, detail}``."""
    try:
        return operation()
    except _HttpError as exc:
        return _error_response(exc.status_code, exc.code, exc.detail)
    except service.ValidationError as exc:
        return _error_response(400, "invalid_connection", str(exc))
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
        return _error_response(400, "invalid_request", f"requisicao invalida: {_clean(exc)}")


def _as_payload(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return dict(body)
    raise _HttpError(
        400,
        "invalid_body",
        "corpo invalido: esperado um objeto JSON com os campos da conexao "
        "(id, engine, environment, host)",
    )


# --------------------------------------------------------------------------
# 4.1 CRUD de conexões
# --------------------------------------------------------------------------


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Row -> public payload: ``secret_ref`` + ``secret_state``, never a value."""
    return service.connection_view(dict(row), resolver=get_secret_resolver())


@router.get("/connections")
def list_connections() -> Any:
    """``GET /connections`` — all connections, no secret value anywhere."""
    return _run(
        lambda: {
            "ok": True,
            "connections": [_view(row) for row in service.list_connections(get_store())],
        }
    )


@router.post("/connections")
def create_connection(payload: Any = Body(None)) -> Any:
    """``POST /connections`` — 201 on create, 409 when the ``id`` exists."""

    def operation() -> Any:
        conn = get_store()
        body = _as_payload(payload)
        connection_id = str(body.get("id") or "").strip()
        if service.get_connection(conn, connection_id):
            raise _HttpError(
                409,
                "connection_exists",
                f"ja existe uma conexao com o id '{connection_id}'; "
                "use PUT /connections/{id} para atualizar",
            )
        row = service.upsert_connection(conn, body)
        _notify_connection_changed("created", row["id"])
        return JSONResponse(
            status_code=201, content={"ok": True, "connection": _view(row)}
        )

    return _run(operation)


@router.put("/connections/{connection_id}")
def upsert_connection_route(connection_id: str, payload: Any = Body(None)) -> Any:
    """``PUT /connections/{id}`` — update/upsert; the path ``id`` always wins."""

    def operation() -> Any:
        conn = get_store()
        body = _as_payload(payload)
        body["id"] = connection_id
        row = service.upsert_connection(conn, body)
        _notify_connection_changed("updated", row["id"])
        return {"ok": True, "connection": _view(row)}

    return _run(operation)


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: str) -> Any:
    """``DELETE /connections/{id}`` — connection plus its cascaded log rows."""

    def operation() -> Any:
        conn = get_store()
        if not service.delete_connection(conn, connection_id):
            raise _HttpError(
                404, "connection_not_found", f"conexao '{connection_id}' nao existe"
            )
        _notify_connection_changed("deleted", connection_id)
        return {"ok": True}

    return _run(operation)


# --------------------------------------------------------------------------
# 4.2 teste de conexão (cada teste vira linha no log: source "ui", statement NULL)
# --------------------------------------------------------------------------


def _log_connection_test(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    result: Mapping[str, Any],
) -> str:
    """Record the test — success *or* failure (design decision 10)."""
    ok = bool(result.get("ok"))
    return service.append_query_log(
        conn,
        connection_id=str(row.get("id")),
        engine=str(row.get("engine")),
        environment=str(row.get("environment")),
        statement=None,
        source=SOURCE_UI,
        status="ok" if ok else "error",
        duration_ms=result.get("latency_ms"),
        error=None if ok else str(result.get("error") or "falha no teste de conexao"),
    )


@router.post("/connections/{connection_id}/test")
def test_connection_route(connection_id: str) -> Any:
    """``POST /connections/{id}/test`` — probe the real database and log it."""

    def operation() -> Any:
        conn = get_store()
        row = service.get_connection(conn, connection_id)
        if not row:
            raise _HttpError(
                404,
                "connection_not_found",
                f"conexao '{connection_id}' nao existe; crie-a antes de testar",
            )
        result = service.test_connection(row, get_driver_registry(), get_secret_resolver())
        _log_connection_test(conn, row, result)
        return dict(result)

    return _run(operation)


# --------------------------------------------------------------------------
# 4.3 leitura do log de consultas
# --------------------------------------------------------------------------


@router.get("/queries")
def list_queries(
    limit: int = 100,
    connection_id: str | None = None,
    since_id: int | None = None,
) -> Any:
    """``GET /queries`` — newest first, ``{ queries: [...] }``."""

    def operation() -> Any:
        if limit < 1:
            raise _HttpError(400, "invalid_limit", "invalid limit: esperado inteiro >= 1")
        rows = service.list_query_log(
            get_store(), limit=limit, connection_id=connection_id, since_id=since_id
        )
        return {"ok": True, "queries": rows}

    return _run(operation)


# --------------------------------------------------------------------------
# 4.4 status consolidado
# --------------------------------------------------------------------------


@router.get("/status")
def get_status(session_id: str | None = None, limit: int = 100) -> Any:
    """``GET /status`` — ``{ active, connections, last_seq, counters }``."""

    def operation() -> Any:
        snapshot = service.status_snapshot(
            get_store(), session_id=session_id, limit=limit
        )
        snapshot["ok"] = True
        return snapshot

    return _run(operation)


# --------------------------------------------------------------------------
# 1.3 rota de sanidade do mount
# --------------------------------------------------------------------------


@router.get("/ping")
def ping() -> Any:
    return {"ok": True, "plugin": PLUGIN_ID}
