"""Agent tools of the ``database-connector`` plugin (tasks 3.1–3.4).

Three tools — :func:`db_list_connections`, :func:`db_query` and
:func:`db_redis_command` — plus the two pieces that make them safe to expose to
a model (design decisions 3, 6, 7 and 10):

* :func:`guarded_handler` wraps every handler before registration: the answer is
  **always a JSON string** and an unexpected exception becomes
  ``{"ok": false, "code": "db_error", "error"}`` truncated and without a
  traceback (the contract of the tool SDK — a handler that raises breaks the
  agent);
* the read policy lives here, but its refusal happens **inside** the single
  cycle of :func:`service.run_query` (through the ``guard`` hook): the log then
  holds its ``running -> error`` line and ``query.failed`` is emitted, so a
  refused statement is never silently swallowed — and never reaches the driver;
* the Redis allowlist is decided **before** the cycle: a refused command does
  not even open a log line, because nothing was sent to the server.

Nothing here re-implements the cycle, the log or the events: the handlers read
the store (for the list) and call :func:`service.run_query` once.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import service
from drivers import DriverRegistry

#: Toolset every tool of this plugin belongs to (namespaced by the host).
TOOLSET = "database-connector"

#: Note carried by every ``driver_missing`` answer (design decision 6/R1b): the
#: driver installed in the middle of a live process is not imported again, so
#: the gateway has to be restarted.
NOTE_RESTART = "reinicie o gateway depois de instalar"

#: ``max_rows``: the cycle owns the cap; the tools only document it.
MAX_ROWS_DEFAULT = service.DEFAULT_MAX_ROWS
MAX_ROWS_CAP = service.MAX_ROWS_CAP

#: Allowlist fechada de leitura do Redis (spec ``query-policy`` -> "Allowlist de
#: comandos Redis"): fora ficam os caros (``KEYS``, ``MEMORY``, ``OBJECT``,
#: ``CLIENT``) e os de escrita/administração (``CONFIG``, ``FLUSH*``, ``AUTH``,
#: ``SCRIPT``, ``EVAL``, ``DEBUG``, ``SHUTDOWN``, ``CLUSTER``, ``REPLICAOF``).
REDIS_ALLOWLIST = frozenset(
    {
        "GET",
        "MGET",
        "STRLEN",
        "EXISTS",
        "TYPE",
        "HGET",
        "HMGET",
        "HGETALL",
        "HKEYS",
        "HVALS",
        "HLEN",
        "LRANGE",
        "LLEN",
        "LINDEX",
        "SMEMBERS",
        "SCARD",
        "SISMEMBER",
        "ZRANGE",
        "ZCARD",
        "ZSCORE",
        "SCAN",
        "TTL",
        "PTTL",
        "DBSIZE",
        "INFO",
    }
)

#: Only a read statement enters a driver: ``SELECT`` or a read CTE (``WITH``).
SQL_ALLOWED_LEAD = frozenset({"select", "with"})

#: Everything else — escrita (``UPDATE``/``DELETE``/``INSERT``/``MERGE``), DDL
#: (``CREATE``/``ALTER``/``DROP``/``TRUNCATE``) e sessão (``SET``/``EXEC``) — is
#: refused here, before the driver, with ``code: "read_only"``.
SQL_WRITE_HINT = (
    "apenas leitura: a tool aceita uma unica statement SELECT (ou CTE WITH ... SELECT)"
)

_LEAD_RE = re.compile(r"[(\s]*([A-Za-z_]+)")
_TRAILING_SEMICOLON_RE = re.compile(r";\s*$")


class QueryPolicyError(RuntimeError):
    """Refusal of the read policy — carries the stable ``read_only`` code.

    Raised by the ``guard`` handed to :func:`service.run_query`, so the refusal
    is classified by the cycle like any other failure (design decision 6) and
    the statement never reaches the driver.
    """

    code = service.CODE_READ_ONLY


# --------------------------------------------------------------------------
# 3.1 wrapper à prova de exceção + serialização
# --------------------------------------------------------------------------


def internal_failure(exc: BaseException) -> dict[str, Any]:
    """``{ok: false, code: "db_error", error}`` — truncado e sem stack trace."""
    try:
        message = service.clean_driver_error(exc)
    except Exception:  # noqa: BLE001 - até aqui nada pode levantar
        message = str(exc)
    message = (message or type(exc).__name__).strip()
    return {
        "ok": False,
        "code": service.CODE_DB_ERROR,
        "error": message[: service.ERROR_MAX_CHARS] or "falha interna na tool",
    }


def to_json(payload: Any) -> str:
    """Render ``payload`` as JSON (never raises; never returns non-JSON)."""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - payload não serializável
        return json.dumps(internal_failure(exc), ensure_ascii=False)


def guarded_handler(handler: Callable[..., Any]) -> Callable[..., str]:
    """Wrap ``handler`` so it always answers a JSON string and never raises."""

    @functools.wraps(handler)
    def guarded(args: Any = None, **kwargs: Any) -> str:
        try:
            payload = handler(args if isinstance(args, Mapping) else {}, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the envelope is the contract
            payload = internal_failure(exc)
        return to_json(payload)

    return guarded


def check_connections_configured() -> bool:
    """``check_fn`` of every tool: **False** withdraws them ("needs configuration").

    No connection enabled means there is nothing to list or to query — the host
    then shows "needs configuration" instead of letting the agent call the tool
    and read a cryptic error (design decision 7).
    """
    try:
        rows = service.list_connections(_store) if _store is not None else []
    except Exception:  # noqa: BLE001 - um store indisponível nunca derruba o host
        return False
    return any(is_enabled(row) for row in rows)


# --------------------------------------------------------------------------
# providers — o que ``register`` pluga (store, config, emissor, segredo)
# --------------------------------------------------------------------------

_store: Any = None
_config_getter: Callable[[str, Any], Any] | None = None
_event_emitter: Callable[..., Any] | None = None
_secret_resolver: Callable[[str], Any] | None = None
_registry: DriverRegistry | None = None


def set_store(store: Any) -> None:
    """Plug the store opened by :func:`service.init_store`."""
    global _store
    _store = store


def get_store() -> Any:
    return _store


def set_config_getter(getter: Callable[[str, Any], Any] | None) -> None:
    """Plug ``ctx.get_config`` (the host's own mechanism for ``log_statement``)."""
    global _config_getter
    _config_getter = getter if callable(getter) else None
    service.set_config_getter(_config_getter)


def set_event_emitter(emitter: Callable[..., Any] | None) -> None:
    """Plug the emitter of ``query.*`` events (host broadcast, when available)."""
    global _event_emitter
    _event_emitter = emitter if callable(emitter) else None
    service.set_query_event_emitter(_event_emitter)


def set_secret_resolver(resolver: Callable[[str], Any] | None) -> None:
    """Plug the secret resolver (``ctx.get_secret``); ``None`` -> host default."""
    global _secret_resolver
    _secret_resolver = resolver if callable(resolver) else None


def set_registry(registry: DriverRegistry | None) -> None:
    """Plug the driver registry (tests inject fakes; the host detects)."""
    global _registry
    _registry = registry


def reset_providers() -> None:
    """Drop every wiring (used by the tests to keep them independent)."""
    global _store, _config_getter, _event_emitter, _secret_resolver, _registry
    _store = None
    _config_getter = None
    _event_emitter = None
    _secret_resolver = None
    _registry = None
    service.set_config_getter(None)
    service.set_query_event_emitter(None)


def _provided(kwargs: Mapping[str, Any], name: str, fallback: Any) -> Any:
    """Injection wins over the wired provider (tests, ``ctx.dispatch_tool``)."""
    value = kwargs.get(name)
    return fallback if value is None else value


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def args_of(args: Any) -> Mapping[str, Any]:
    """The arguments of the call, always a mapping."""
    return args if isinstance(args, Mapping) else {}


def is_enabled(row: Mapping[str, Any]) -> bool:
    """True when the connection is enabled (default of the schema: enabled)."""
    return bool(row.get("enabled", 1))


def _failure(code: str, error: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "ok": False,
        "code": code,
        "error": str(error)[: service.ERROR_MAX_CHARS],
    }
    payload.update(extra)
    return payload


def _no_store() -> dict[str, Any]:
    return _failure(
        service.CODE_DB_ERROR,
        "store do plugin nao configurado: o registro do plugin nao foi concluido",
    )


def _connection_ref(connection_row: Mapping[str, Any]) -> dict[str, Any]:
    """Minimal identification of the connection in every answer."""
    return {
        "id": connection_row.get("id"),
        "engine": connection_row.get("engine"),
        "environment": connection_row.get("environment"),
    }


# --------------------------------------------------------------------------
# política read-only (design decision 4a): filtro antes do driver
# --------------------------------------------------------------------------


def leading_keyword(statement: str) -> str:
    """First keyword of the statement, lowercase (``""`` when there is none)."""
    match = _LEAD_RE.match(str(statement))
    return match.group(1).lower() if match else ""


def refuse_statement(statement: Any) -> str | None:
    """Reason why ``statement`` must not reach the driver (``None`` when ok).

    Camadas do filtro (spec ``query-policy``):

    * comentários ``--`` e ``/* */`` — o driver executa lote escondido neles;
    * ``;`` seguido de outra statement (um único terminador no fim é aceito);
    * o primeiro keyword tem que ser ``SELECT`` ou ``WITH`` (CTE de leitura) —
      escrita, DDL e comando de sessão/transação caem aqui.
    """
    if statement is None or not str(statement).strip():
        return "statement vazia: informe uma unica consulta de leitura"
    text = str(statement)
    if "--" in text or "/*" in text:
        return "comentario nao aceito (-- ou /* */): reescreva a consulta sem comentario"
    single = _TRAILING_SEMICOLON_RE.sub("", text.strip())
    if ";" in single:
        return "apenas uma statement por chamada (';' inicia um lote)"
    lead = leading_keyword(single)
    if lead not in SQL_ALLOWED_LEAD:
        return f"{SQL_WRITE_HINT} (recebido '{lead or '?'}')"
    return None


def sql_read_only_guard(**cycle: Any) -> None:
    """``guard`` of the cycle: refuse a non-read statement **before** the driver.

    Called inside :func:`service.run_query`, already after the ``running`` line
    and ``query.started``: raising here makes the cycle finish the same line as
    ``error`` and emit ``query.failed`` with ``code: "read_only"`` — the policy
    is logged like any other failure and the statement never runs.
    """
    reason = refuse_statement(cycle.get("statement"))
    if reason:
        raise QueryPolicyError(reason)


def redis_command_allowed(command: Any) -> str | None:  # noqa: D401
    """Reason why ``command`` is refused (``None`` when it is in the allowlist)."""
    name = str(command or "").strip().upper()
    if not name:
        return "comando nao informado: informe um comando de leitura do Redis"
    if name not in REDIS_ALLOWLIST:
        allowed = ", ".join(sorted(REDIS_ALLOWLIST))
        return f"comando '{name}' fora da allowlist de leitura; permitidos: {allowed}"
    return None


# --------------------------------------------------------------------------
# 3.2 db_list_connections
# --------------------------------------------------------------------------


def connection_summary(
    connection_row: Mapping[str, Any], registry: DriverRegistry
) -> dict[str, Any]:
    """Public view of a connection: metadata + ``driver_missing`` — no secret.

    ``secret_ref`` (and anything else a row carries, like ``username``) stays
    out of the answer: the model has to ask for the connection by ``id``.
    """
    engine = connection_row.get("engine")
    summary = {
        "id": connection_row.get("id"),
        "label": connection_row.get("label"),
        "engine": engine,
        "environment": connection_row.get("environment"),
        "host": connection_row.get("host"),
        "port": connection_row.get("port"),
        "database": connection_row.get("database"),
        "read_only": bool(connection_row.get("read_only", 1)),
    }
    if _driver_missing(engine, registry):
        summary["driver_missing"] = True
    return summary


def _driver_missing(engine: Any, registry: DriverRegistry) -> bool:
    """True when ``engine`` has no driver in this host (no import happens)."""
    try:
        return not bool(registry.status(engine)["available"])
    except Exception:  # noqa: BLE001 - engine desconhecida não derruba a lista
        return False


def db_list_connections(args: Any = None, **kwargs: Any) -> dict[str, Any]:
    """List the enabled connections (filtering by ``environment`` when asked)."""
    params = args_of(args)
    store = _provided(kwargs, "store", _store)
    if store is None:
        return _no_store()
    registry = _provided(kwargs, "registry", _registry) or DriverRegistry()

    wanted = params.get("environment")
    wanted = str(wanted).strip().lower() if wanted else None

    connections = []
    for row in service.list_connections(store):
        if not is_enabled(row):
            continue
        if wanted and str(row.get("environment", "")).strip().lower() != wanted:
            continue
        connections.append(connection_summary(row, registry))

    return {"ok": True, "count": len(connections), "connections": connections}


# --------------------------------------------------------------------------
# 3.3 db_query
# --------------------------------------------------------------------------


def db_query(args: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Run **one** read statement: the whole policy plus the single cycle."""
    params = args_of(args)
    store = _provided(kwargs, "store", _store)
    if store is None:
        return _no_store()

    connection_id = str(params.get("connection_id") or "").strip()
    if not connection_id:
        return _failure(
            service.CODE_NOT_FOUND,
            "connection_id nao informado: chame db_list_connections e use o 'id'",
        )

    row = service.get_connection(store, connection_id)
    if row is None or not is_enabled(row):
        # Antes do ciclo: nada é logado e nenhum evento é emitido.
        return _failure(
            service.CODE_NOT_FOUND,
            f"conexao '{connection_id}' nao encontrada ou desabilitada; "
            "chame db_list_connections para ver os ids disponiveis",
        )

    result = service.run_query(
        store,
        row,
        params.get("statement"),
        MAX_ROWS_DEFAULT if params.get("max_rows") is None else params.get("max_rows"),
        service.SOURCE_AGENT,
        kwargs.get("session_id"),
        conversation_id=kwargs.get("conversation_id"),
        registry=_provided(kwargs, "registry", _registry),
        resolver=_provided(kwargs, "resolver", _secret_resolver),
        emitter=_provided(kwargs, "emitter", _event_emitter),
        config_getter=_provided(kwargs, "config_getter", _config_getter),
        driver=kwargs.get("driver"),
        guard=sql_read_only_guard,
    )

    if result.get("ok"):
        return {
            "ok": True,
            "rows": result.get("rows"),
            "row_count": result.get("row_count"),
            "truncated": bool(result.get("truncated")),
            "duration_ms": result.get("duration_ms"),
            "connection": _connection_ref(row),
        }

    failure = _failure(
        result.get("code") or service.CODE_DB_ERROR,
        result.get("error") or "falha na consulta",
        duration_ms=result.get("duration_ms"),
        connection=_connection_ref(row),
    )
    if failure["code"] == service.CODE_DRIVER_MISSING:
        # Pronto para executar + a nota de que o processo vivo não reimporta.
        failure["install"] = result.get("install") or ""
        failure["note"] = NOTE_RESTART
    return failure


# --------------------------------------------------------------------------
# 3.4 db_redis_command
# --------------------------------------------------------------------------


def db_redis_command(args: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Run **one** read command of the Redis allowlist."""
    params = args_of(args)
    command = str(params.get("command") or "").strip().upper()

    # Allowlist antes de qualquer coisa: sem linha "running", sem servidor.
    refused = redis_command_allowed(command)
    if refused:
        return _failure(service.CODE_READ_ONLY, refused)

    store = _provided(kwargs, "store", _store)
    if store is None:
        return _no_store()

    connection_id = str(params.get("connection_id") or "").strip()
    if not connection_id:
        return _failure(
            service.CODE_NOT_FOUND,
            "connection_id nao informado: chame db_list_connections e use o 'id'",
        )

    row = service.get_connection(store, connection_id)
    if row is None or not is_enabled(row):
        return _failure(
            service.CODE_NOT_FOUND,
            f"conexao '{connection_id}' nao encontrada ou desabilitada; "
            "chame db_list_connections para ver os ids disponiveis",
        )

    raw_args = params.get("args") or []
    if isinstance(raw_args, (str, bytes)):
        raw_args = [raw_args]
    else:
        raw_args = list(raw_args) if isinstance(raw_args, Sequence) else [raw_args]

    result = service.run_query(
        store,
        row,
        command,
        MAX_ROWS_DEFAULT,
        service.SOURCE_AGENT,
        kwargs.get("session_id"),
        args=tuple(raw_args),
        conversation_id=kwargs.get("conversation_id"),
        registry=_provided(kwargs, "registry", _registry),
        resolver=_provided(kwargs, "resolver", _secret_resolver),
        emitter=_provided(kwargs, "emitter", _event_emitter),
        config_getter=_provided(kwargs, "config_getter", _config_getter),
        driver=kwargs.get("driver"),
    )

    if result.get("ok"):
        payload: dict[str, Any] = {
            "ok": True,
            # Já truncado em 64 KiB pelo driver (spec query-policy).
            "value": result.get("value"),
            "duration_ms": result.get("duration_ms"),
            "connection": _connection_ref(row),
        }
        if result.get("truncated"):
            payload["truncated"] = True
        if result.get("row_count") is not None:
            payload["row_count"] = result.get("row_count")
        return payload

    failure = _failure(
        result.get("code") or service.CODE_DB_ERROR,
        result.get("error") or "falha no comando",
        duration_ms=result.get("duration_ms"),
        connection=_connection_ref(row),
    )
    if failure["code"] == service.CODE_DRIVER_MISSING:
        failure["install"] = result.get("install") or ""
        failure["note"] = NOTE_RESTART
    return failure


# --------------------------------------------------------------------------
# schemas + registro (3.1)
# --------------------------------------------------------------------------

SCHEMA_LIST_CONNECTIONS: dict[str, Any] = {
    "name": "db_list_connections",
    "description": (
        "Lista as conexões de banco habilitadas (id, label, engine, environment, host, "
        "port, database, read_only) sem nunca expor segredo. Use antes de consultar para "
        "descobrir o connection_id; conexões marcadas com driver_missing não têm driver "
        "instalado neste host. Filtro opcional por environment (prod, homolog, local)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "environment": {
                "type": "string",
                "description": "Filtro opcional: prod, homolog ou local.",
                "enum": ["prod", "homolog", "local"],
            },
        },
        "required": [],
    },
}

SCHEMA_QUERY: dict[str, Any] = {
    "name": "db_query",
    "description": (
        "Executa UMA consulta SQL somente-leitura (SELECT ou CTE WITH ... SELECT) numa "
        "conexão existente e devolve as linhas. Escrita (UPDATE/DELETE/INSERT/MERGE), DDL "
        "(CREATE/ALTER/DROP/TRUNCATE), comando de sessão (SET/EXEC), lote com ';' e "
        "comentário são recusados com code read_only. max_rows padrão 200, teto 1000."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "connection_id": {
                "type": "string",
                "description": "Id da conexão (db_list_connections).",
            },
            "statement": {
                "type": "string",
                "description": "Uma única statement de leitura (SELECT/WITH).",
            },
            "max_rows": {
                "type": "integer",
                "description": "Máximo de linhas devolvidas (padrão 200, teto 1000).",
            },
        },
        "required": ["connection_id", "statement"],
    },
}

SCHEMA_REDIS_COMMAND: dict[str, Any] = {
    "name": "db_redis_command",
    "description": (
        "Executa UM comando Redis de leitura da allowlist (GET, MGET, STRLEN, EXISTS, TYPE, "
        "HGET, HMGET, HGETALL, HKEYS, HVALS, HLEN, LRANGE, LLEN, LINDEX, SMEMBERS, SCARD, "
        "SISMEMBER, ZRANGE, ZCARD, ZSCORE, SCAN, TTL, PTTL, DBSIZE, INFO). Fora da lista "
        "(KEYS, CONFIG, FLUSHALL, EVAL, ...) é recusado com code read_only sem contato com "
        "o servidor. O valor volta truncado em 64 KiB."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "connection_id": {
                "type": "string",
                "description": "Id da conexão Redis (db_list_connections).",
            },
            "command": {
                "type": "string",
                "description": "Comando de leitura da allowlist (ex.: GET, HGETALL).",
            },
            "args": {
                "type": "array",
                "description": "Argumentos do comando (ex.: a chave de GET).",
                "items": {},
            },
        },
        "required": ["connection_id", "command"],
    },
}

#: The three tools actually registered — the manifest's ``provides_tools``
#: (task 3.5) has to mirror this list exactly.
TOOL_DEFINITIONS: tuple[tuple[str, dict[str, Any], Callable[..., Any]], ...] = (
    ("db_list_connections", SCHEMA_LIST_CONNECTIONS, db_list_connections),
    ("db_query", SCHEMA_QUERY, db_query),
    ("db_redis_command", SCHEMA_REDIS_COMMAND, db_redis_command),
)

TOOL_NAMES: tuple[str, ...] = tuple(name for name, _schema, _handler in TOOL_DEFINITIONS)


def register_tools(ctx: Any) -> list[str]:
    """Wire the providers and register the three tools — lazy, never raising.

    Called once by ``register(ctx)`` with the imports **inside** the function:
    a failure here would disable the whole plugin, so each registration is
    isolated and the wired providers stay usable even when the host refuses a
    tool (name collision, for instance).
    """
    try:
        set_store(service.init_store(ctx))
    except Exception:  # noqa: BLE001 - sem store as tools respondem configuracao
        set_store(None)

    config_getter = getattr(ctx, "get_config", None)
    if callable(config_getter):
        set_config_getter(config_getter)

    secret_resolver = getattr(ctx, "get_secret", None)
    if callable(secret_resolver):
        set_secret_resolver(secret_resolver)

    emitter = getattr(ctx, "broadcast_plugin_event", None)
    if callable(emitter):
        set_event_emitter(emitter)

    registered: list[str] = []
    for name, schema, handler in TOOL_DEFINITIONS:
        try:
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=guarded_handler(handler),
                check_fn=check_connections_configured,
                description=schema.get("description", ""),
            )
        except Exception:  # noqa: BLE001 - uma tool não derruba as outras
            continue
        registered.append(name)
    return registered
