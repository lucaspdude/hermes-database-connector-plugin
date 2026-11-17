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

import importlib
import json
import sqlite3
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import drivers
import service

PLUGIN_ID = "database-connector"


async def _read_body(request: Request) -> bytes:
    """Raw body as a dependency: the framework parses nothing.

    A router cannot own a ``RequestValidationError`` handler, and letting
    FastAPI parse the body answers a malformed JSON with its own 422 (echoing
    ``input``). Reading it here and decoding it inside ``_run`` keeps every
    failure in the plugin's ``{error, detail}`` path — and the raw payload is
    never echoed back.
    """
    return await request.body()


def _decode_body(raw: bytes) -> Any:
    """Decode the JSON body inside ``_run`` (never echo it in the error)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _HttpError(
            400, "invalid_body", "corpo invalido: o JSON enviado nao pode ser lido"
        ) from None

#: Log source of everything triggered by the REST layer (spec backend-api).
SOURCE_UI = "ui"

#: Event emitted after every CRUD operation (task 4.6, spec backend-api).
EVENT_CONNECTION_CHANGED = "connection.changed"

router = APIRouter()


# --------------------------------------------------------------------------
# providers — what ``register(ctx)`` plugs in later (wiring do host Hermes)
# --------------------------------------------------------------------------

_store: sqlite3.Connection | None = None
_store_factory: Callable[[], Any] | None = None
_driver_registry: drivers.DriverRegistry | None = None
_secret_resolver: Callable[[str], Any] | None = None
_connection_change_emitter: Callable[[str, str, Mapping[str, Any]], Any] | None = None


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
    """Wiring for the Hermes host: open the plugin store and remember it.

    When ``ctx`` exposes ``broadcast_plugin_event`` it becomes the emitter of
    ``connection.changed`` (task 4.6); otherwise the lazy resolution in
    :func:`resolve_broadcast` takes over.
    """
    conn = service.init_store(ctx)
    set_store(conn)
    candidate = getattr(ctx, "broadcast_plugin_event", None)
    if callable(candidate):
        set_connection_change_emitter(candidate)
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


def set_connection_change_emitter(
    emitter: Callable[[str, str, Mapping[str, Any]], Any] | None,
) -> None:
    """Inject the ``connection.changed`` emitter (tests use a fake/listener).

    The signature is the host's one: ``emitter(plugin_id, event, payload)``,
    i.e. ``broadcast_plugin_event("database-connector", "connection.changed",
    {...})``.
    """
    global _connection_change_emitter
    _connection_change_emitter = emitter


def get_connection_change_emitter() -> Callable[..., Any] | None:
    return _connection_change_emitter


#: Where the host may expose ``broadcast_plugin_event``. Tried in order; the
#: import is **lazy** (inside the emit) because none of these modules exists
#: outside a running Hermes — importing at module load would break the plugin.
BROADCAST_SOURCES: tuple[tuple[str, str], ...] = (
    ("hermes.gateway.events", "broadcast_plugin_event"),
    ("hermes.gateway", "broadcast_plugin_event"),
    ("hermes.events", "broadcast_plugin_event"),
    ("gateway.events", "broadcast_plugin_event"),
)


def resolve_broadcast() -> Callable[..., Any] | None:
    """Return the host's ``broadcast_plugin_event``, or ``None`` outside Hermes."""
    for module_name, attribute in BROADCAST_SOURCES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - module simply not present in tests
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate
    return None


def _notify_connection_changed(action: str, connection_id: str) -> bool:
    """Emit ``connection.changed`` **after** the operation succeeded.

    Called by every CRUD handler. First the injected emitter (tests/listener),
    then the real ``broadcast_plugin_event`` resolved lazily from the host.
    A failure here NEVER propagates: the route response is already decided and
    the Gateway must not lose a 201 because the bus is down (spec backend-api:
    "eventos apenas invalidam" — REST remains the source of truth).
    """
    payload = {"connection_id": str(connection_id), "action": action}
    try:
        emitter = get_connection_change_emitter() or resolve_broadcast()
        if emitter is None:
            return False
        emitter(PLUGIN_ID, EVENT_CONNECTION_CHANGED, payload)
        return True
    except Exception:  # noqa: BLE001 - emissao falha nao derruba a resposta
        return False


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
        return _unexpected(exc)


def _unexpected(exc: BaseException) -> JSONResponse:
    """500 clean: no traceback and no exception **message** (it could carry a
    DSN or a secret value). Only the exception type is exposed, which helps the
    operator without leaking data (spec backend-api, task 4.7)."""
    return _error_response(
        500,
        "internal_error",
        "erro interno no plugin database-connector "
        f"({type(exc).__name__}); consulte os logs do gateway",
    )


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
def create_connection(raw: bytes = Depends(_read_body)) -> Any:
    """``POST /connections`` — 201 on create, 409 when the ``id`` exists."""

    def operation() -> Any:
        conn = get_store()
        body = _as_payload(_decode_body(raw))
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
def upsert_connection_route(
    connection_id: str, raw: bytes = Depends(_read_body)
) -> Any:
    """``PUT /connections/{id}`` — update/upsert; the path ``id`` always wins."""

    def operation() -> Any:
        conn = get_store()
        body = _as_payload(_decode_body(raw))
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


def _parse_int(field: str, raw: str | None, *, minimum: int, default: int) -> int:
    """Parse a query parameter **inside the route** (never let the framework
    raise for us): an ``APIRouter`` cannot own exception handlers, so anything
    the framework validates would escape the ``{error, detail}`` contract."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _HttpError(
            400, "invalid_request", f"requisicao invalida: {field}: esperado inteiro >= {minimum}"
        ) from None
    if parsed < minimum:
        raise _HttpError(
            400, "invalid_request", f"requisicao invalida: {field}: esperado inteiro >= {minimum}"
        )
    return parsed


@router.get("/queries")
def list_queries(
    limit: str | None = Query(None),
    connection_id: str | None = None,
    since_id: str | None = Query(None),
) -> Any:
    """``GET /queries`` — newest first, ``{ queries: [...] }``."""

    def operation() -> Any:
        rows = service.list_query_log(
            get_store(),
            limit=_parse_int("limit", limit, minimum=1, default=100),
            connection_id=connection_id,
            since_id=_parse_int("since_id", since_id, minimum=0, default=0) or None,
        )
        return {"ok": True, "queries": rows}

    return _run(operation)


# --------------------------------------------------------------------------
# 4.4 status consolidado
# --------------------------------------------------------------------------


@router.get("/status")
def get_status(
    session_id: str | None = None, limit: str | None = Query(None)
) -> Any:
    """``GET /status`` — ``{ active, connections, last_seq, counters }``."""

    def operation() -> Any:
        snapshot = service.status_snapshot(
            get_store(),
            session_id=session_id,
            limit=_parse_int("limit", limit, minimum=1, default=100),
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


# --------------------------------------------------------------------------
# 4.5 health de drivers por engine (sem importar nada: so find_spec)
# --------------------------------------------------------------------------


@router.get("/health")
def health() -> Any:
    """``GET /health`` — ``{ ok, engines }`` with per-engine driver availability.

    The registry decides availability with ``importlib.util.find_spec`` only, so
    a missing driver never breaks the other engines nor the plugin load; each
    entry carries the ready-to-run ``install`` command (uv path resolved).
    """

    def operation() -> Any:
        return {"ok": True, "engines": get_driver_registry().health()}

    return _run(operation)


# --------------------------------------------------------------------------
# 4.7 handlers: toda saida de erro vira { error, detail }, sem traceback
# --------------------------------------------------------------------------


def _validation_detail(exc: RequestValidationError) -> str:
    """Render FastAPI's validation errors as one actionable Portuguese line.

    Only field + message are used — never ``input`` (Pydantic echoes the value
    there, and that value may be a secret or a full DSN).
    """
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(
            str(part) for part in error.get("loc", ()) if part not in {"query", "body", "path"}
        )
        message = str(error.get("msg") or "valor invalido")
        parts.append(f"{location or 'corpo'}: {message}")
    return "requisicao invalida: " + "; ".join(parts)


def _validation_error_handler(_request: Any, exc: RequestValidationError) -> JSONResponse:
    return _error_response(400, "invalid_request", _validation_detail(exc))


def _http_error_handler(_request: Any, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(
        exc.status_code, "http_error"
    )
    detail = str(exc.detail) if exc.detail else "requisicao nao atendida"
    return _error_response(exc.status_code, code, detail)


def _unexpected_error_handler(_request: Any, exc: Exception) -> JSONResponse:
    return _unexpected(exc)


#: Path of the catch-all route below (never counted as a "known" route).
CATCH_ALL_PATH = "/{unknown_path:path}"

#: Methods probed to tell a 404 from a 405 inside the catch-all route.
_PROBE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def _known_route_segments() -> list[tuple[list[str], list[str]]]:
    """``(segments, methods)`` of every real route of this router.

    Read from the router itself: the host may mount it under any prefix (and
    recent FastAPI versions keep included routers lazy), so matching is done on
    the **tail** of the requested path.
    """
    known: list[tuple[list[str], list[str]]] = []
    for route in router.routes:
        route_path = getattr(route, "path", None)
        if not route_path or route_path == CATCH_ALL_PATH:
            continue
        segments = route_path.strip("/").split("/") if route_path.strip("/") else []
        known.append((segments, [m for m in getattr(route, "methods", None) or ()]))
    return known


def _routes_matching(request: Request) -> list[str]:
    """Methods the router accepts for the requested path (405 vs 404)."""
    path = request.scope.get("path") or "/"
    requested = path.strip("/").split("/") if path.strip("/") else []
    found: list[str] = []
    for segments, methods in _known_route_segments():
        if not segments or len(segments) > len(requested):
            continue
        tail = requested[-len(segments):]
        if not all(
            segment.startswith("{") or segment == got for segment, got in zip(segments, tail)
        ):
            continue
        for method in methods:
            if method not in found:
                found.append(method)
    return found


@router.api_route(CATCH_ALL_PATH, methods=list(_PROBE_METHODS), include_in_schema=False)
def _catch_all(request: Request) -> Any:
    """Whatever this router does not own still answers ``{error, detail}``.

    Registered **last**, so every real route wins. It is what makes the 4xx
    contract true in the host (which mounts the router into *its* app and
    never sees a plugin-level exception handler): unknown paths and wrong
    methods inside the plugin prefix are answered here, not by the framework.
    """
    path = request.scope.get("path") or "/"
    allowed = _routes_matching(request)
    if allowed:
        return _error_response(
            405,
            "method_not_allowed",
            f"metodo {request.method} nao permitido em '{path}'; use {', '.join(allowed)}",
        )
    return _error_response(
        404, "not_found", f"rota desconhecida no plugin database-connector: '{path}'"
    )


def install_error_handlers(app: FastAPI) -> FastAPI:
    """Extend the ``{error, detail}`` standard to the **app** that mounts us.

    Complement, not the guarantee: an ``APIRouter`` cannot carry exception
    handlers (they belong to the app), so every failure inside this plugin is
    already standardised here — route bodies by ``_run``, query/body values by
    parsing them inside the route, and unknown paths/methods by the catch-all
    route above. This helper covers what the host itself raises outside the
    plugin prefix (its own routes, when it opts in).
    """
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(Exception, _unexpected_error_handler)
    return app
