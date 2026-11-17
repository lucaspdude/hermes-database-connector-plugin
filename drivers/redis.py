"""Redis driver (redis-py) — one read command per execution, value capped.

Implements the same uniform interface as :mod:`drivers.postgres` and
:mod:`drivers.sqlserver`, so the query cycle never learns which engine it is
talking to:

* :func:`connect`  -> client (``db_index``/``tls``/timeouts from options);
* :func:`execute`  -> ``{value, row_count, truncated, duration_ms, ...}`` with
  the value truncated at **64 KiB** (spec ``query-policy`` -> "Timeouts e
  limites");
* :func:`probe`    -> ``PING`` (used by ``test_connection``);
* :func:`close`    -> release the client, never raising.

There is no transaction and no server-side READ ONLY here: read safety is the
**allowlist** of the tool (design decision 10), which refuses anything outside
the 25 curated read commands without ever contacting the server.

The module talks to ``redis`` **lazily**: importing ``drivers.redis`` never
imports redis-py, so a host without the dependency keeps working (R1) and the
tests inject a fake module (real instance validation still pending — host
``hostgator``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from drivers import DriverMissingError, install_command

ENGINE = "redis"
DRIVER_MODULE = "redis"

#: Cap of every value returned to the agent (spec query-policy: 64 KiB).
VALUE_MAX_BYTES = 64 * 1024

#: Probe used by ``test_connection``: a PING, no key involved.
PROBE_COMMAND = "PING"

DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_STATEMENT_TIMEOUT_S = 30
DEFAULT_MAX_ROWS = 200

_MISSING = object()
_redis: Any = _MISSING


def set_driver_module(module: Any) -> None:
    """Inject the driver module (tests/fakes) and drop any cached one."""
    global _redis
    _redis = module


def forget_driver_module() -> None:
    """Force the next :func:`_driver` call to re-import."""
    global _redis
    _redis = _MISSING


def _driver() -> Any:
    """Return the redis module, importing it here (never at module load)."""
    global _redis
    if _redis is None:  # convention of the registry: ``None`` forces missing
        raise DriverMissingError(
            ENGINE,
            DRIVER_MODULE,
            install_command(ENGINE),
            f"driver '{DRIVER_MODULE}' for engine '{ENGINE}' is not installed",
        )
    if _redis is _MISSING:
        try:
            import redis as module  # type: ignore[import-not-found]
        except ImportError:
            raise DriverMissingError(
                ENGINE,
                DRIVER_MODULE,
                install_command(ENGINE),
                f"driver '{DRIVER_MODULE}' for engine '{ENGINE}' is not installed",
            ) from None
        _redis = module
    return _redis


# --------------------------------------------------------------------------
# options helpers (kept in sync with drivers.postgres — see comment there)
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


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def connect_timeouts(connection_row: Mapping[str, Any]) -> tuple[float, float]:
    """``(connect_timeout_s, statement_timeout_s)`` — the statement budget is
    what lands on the driver as ``socket_timeout`` (no server-side statement
    timeout exists for Redis)."""
    options = options_of(connection_row)
    return (
        _positive_float(options.get("connect_timeout_s"), DEFAULT_CONNECT_TIMEOUT_S),
        _positive_float(options.get("statement_timeout_s"), DEFAULT_STATEMENT_TIMEOUT_S),
    )


# --------------------------------------------------------------------------
# uniform interface
# --------------------------------------------------------------------------


def connect(connection_row: Mapping[str, Any], secret_value: str | None = None) -> Any:
    """Open a client for ``connection_row`` (no command is issued here).

    ``db_index`` (explicit option) or the connection's ``database`` (Redis has
    no database *name*, only numbered indexes) selects the logical database;
    ``tls: true`` turns on TLS; ``socket_timeout_s``/``socket_connect_timeout``
    bound every single read.
    """
    row = dict(connection_row) if isinstance(connection_row, Mapping) else {}
    options = options_of(row)
    connect_timeout_s, statement_timeout_s = connect_timeouts(row)

    kwargs: dict[str, Any] = {
        "host": str(row.get("host") or ""),
        "port": _positive_int(row.get("port"), 6379),
        "socket_connect_timeout": connect_timeout_s,
        "socket_timeout": _positive_float(
            options.get("socket_timeout_s"), statement_timeout_s
        ),
        # Values come back as text: JSON-friendly and uniform with the engines
        # whose rows are already decoded.
        "decode_responses": _as_bool(options.get("decode_responses"), True),
    }

    database = options.get("db_index")
    if database is None:
        database = row.get("database")
    if database is not None and database != "":
        kwargs["db"] = _positive_int(database, 0)

    if row.get("username"):
        kwargs["username"] = str(row["username"])
    if secret_value is not None:
        kwargs["password"] = secret_value

    # TLS: only what the connection declared — nothing invented here.
    if _as_bool(options.get("tls")):
        kwargs["ssl"] = True
    for key in ("ssl_ca_certs", "ssl_certfile", "ssl_keyfile", "ssl_check_hostname"):
        if options.get(key) is not None:
            kwargs[key] = options[key]

    return _client_factory(_driver())(**kwargs)


def execute(
    client: Any,
    command: str,
    args: Sequence[Any] = (),
    *,
    statement_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Run **one** read command and cap its value at 64 KiB.

    Returns ``{value, row_count, truncated, duration_ms, server_version}`` —
    the same keys the SQL engines carry, so the cycle (and its log) treats all
    engines alike. ``truncated`` is true when the value was cut.

    ``statement_timeout_s`` is accepted for signature uniformity: the client
    already carries its own socket timeout from :func:`connect`.
    """
    started = time.perf_counter()
    callback = getattr(client, "execute_command", None)
    if not callable(callback):  # pragma: no cover - defensive for odd clients
        raise RuntimeError("redis client without execute_command()")
    raw = callback(str(command).upper(), *tuple(args or ()))
    duration_ms = int(round((time.perf_counter() - started) * 1000))

    value, truncated = truncate_value(raw)
    return {
        "value": value,
        "row_count": _row_count(value),
        "truncated": truncated,
        "duration_ms": duration_ms,
        "server_version": server_version(client),
        "command": str(command).upper(),
    }


def probe(client: Any, statement: str = PROBE_COMMAND) -> dict[str, Any]:
    """Minimal probe for ``test_connection``: a single PING."""
    ping = getattr(client, "ping", None)
    if callable(ping):
        ping()
    version = server_version(client)
    return {"server_version": version} if version else {}


def close(client: Any) -> None:
    """Release the client, never raising (used after any error path)."""
    _close_quietly(client)


def server_version(client: Any) -> str | None:
    """Best-effort version read **while the client is open** (never raises).

    Read from an already available attribute/``info`` mapping — never with an
    extra ``INFO`` round trip per query.
    """
    for holder_name in ("info", None):
        try:
            holder = client if holder_name is None else getattr(client, holder_name)
            if callable(holder):  # redis-py exposes ``info()`` as a method
                holder = holder()
            value = _version_in(holder)
        except Exception:  # noqa: BLE001 - closed/blocked client: report nothing
            continue
        if isinstance(value, (str, int, float)) and value:
            return str(value)
    return None


def _version_in(holder: Any) -> Any:
    if holder is None:
        return None
    if isinstance(holder, Mapping):
        return holder.get("redis_version") or holder.get("server_version")
    return getattr(holder, "redis_version", None) or getattr(holder, "server_version", None)


# --------------------------------------------------------------------------
# truncamento do valor (64 KiB)
# --------------------------------------------------------------------------


def truncate_value(
    value: Any, max_bytes: int = VALUE_MAX_BYTES
) -> tuple[Any, bool]:
    """Return ``(value, truncated)`` with the payload capped at ``max_bytes``.

    Bytes are decoded (``errors="replace"``) so the answer is JSON-serialisable;
    containers are serialised to measure their real size and, when they bust the
    cap, delivered as their own (cut) JSON text — the cycle never ships an
    unbounded payload to the agent, the log or an event (spec query-policy).
    """
    normalized = _json_safe(value)
    if isinstance(normalized, str):
        encoded = normalized.encode("utf-8")
        if len(encoded) <= max_bytes:
            return normalized, False
        return encoded[:max_bytes].decode("utf-8", errors="replace"), True

    try:
        serialized = json.dumps(normalized, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - unserialisable object
        serialized = str(normalized)
    encoded = serialized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return normalized, False
    return encoded[:max_bytes].decode("utf-8", errors="replace"), True


def _text(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _json_safe(value: Any) -> Any:
    """Same answer redis-py would give with ``decode_responses=True``."""
    if isinstance(value, (bytes, bytearray)):
        return _text(value)
    if isinstance(value, Mapping):
        return {_text(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def _row_count(value: Any) -> int:
    """How many items the command delivered (1 for a scalar, 0 for nothing)."""
    if value is None:
        return 0
    if isinstance(value, (str, bytes, bytearray)):
        return 1
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    return 1


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _client_factory(module: Any) -> Any:
    """redis-py exposes the client as the ``Redis`` class, not as ``connect``."""
    factory = getattr(module, "Redis", None)
    if callable(factory):
        return factory
    if callable(getattr(module, "connect", None)):  # pragma: no cover - fake style
        return module.connect
    return module


def _close_quietly(obj: Any) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - closing must never mask the result
            pass
