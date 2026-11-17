"""Tests for the REST routes of ``add-plugin-backend`` tasks 4.1–4.4.

The store is a throwaway SQLite file in ``tmp_path`` injected with
``set_store`` — no Hermes runtime, no ``plugin_db``. Drivers come from the fake
registry; ``curl`` via the real gateway is still pending (host ``hostgator``).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import service
from dashboard import plugin_api

PREFIX = "/api/plugins/database-connector"
SECRET_VALUE = "s3nh@SuperSecreta-NAO-DEVE-VAZAR"


def _connection(**overrides):
    payload = {
        "id": "prod-sqlserver",
        "label": "Produção - SQL Server",
        "engine": "sqlserver",
        "environment": "prod",
        "host": "sql.prod.local",
        "port": 1433,
        "database": "HubFinanceira",
        "username": "hermes_ro",
        "secret_ref": "DB_PROD_SQLSERVER_PASSWORD",
    }
    payload.update(overrides)
    return payload


def resolver(_name):
    """Falso resolutor de segredo: o VALOR existe so em memoria."""
    return SECRET_VALUE


# --------------------------------------------------------------------------
# fakes de driver (mesma forma dos testes de 3.1)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, client):
        self.client = client

    def execute(self, statement):
        self.client.executed.append(statement)

    def fetchone(self):
        return (1,)

    def close(self):
        self.client.cursor_closed = True


class _FakeSqlClient:
    def __init__(self, version="16.0.1000", raises=None):
        self.server_version = version
        self.raises = raises
        self.executed: list[str] = []
        self.cursor_closed = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


class _FakeSqlDriver:
    def __init__(self, *, raises=None, version="16.0.1000"):
        self.raises = raises
        self.version = version
        self.clients: list[_FakeSqlClient] = []

    def connect(self, **_kwargs):
        if self.raises:
            raise self.raises
        client = _FakeSqlClient(version=self.version, raises=self.raises)
        self.clients.append(client)
        return client


class _FakeRedisClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pings = 0

    def ping(self):
        self.pings += 1
        return True

    def close(self):
        pass


class _FakeRedisDriver:
    def Redis(self, **kwargs):
        return _FakeRedisClient(**kwargs)


class OperationalError(Exception):
    """Stand-in de ``psycopg.OperationalError`` (cliente fechado)."""


class _PgLikeClient:
    """Client estilo psycopg: **depois de fechado** ``.info`` estoura.

    E o bug do P1: a versao era lida fora do ``close``, entao um probe
    bem-sucedido virava 500 e a linha do log nem chegava a ser gravada.
    Aqui o ``close`` tambem falha — e isso nao pode mudar o resultado.
    """

    def __init__(self, version="16.2"):
        self._version = version
        self.closed = False
        self.executed: list[str] = []

    @property
    def info(self):
        if self.closed:
            raise OperationalError("the connection is closed")
        return _FakeSqlClient(version=self._version)

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True
        raise RuntimeError("falha ao fechar o client (ja fechado)")


class _FixedClientDriver:
    """Driver que devolve sempre o mesmo client (para forcar o cenario)."""

    def __init__(self, client):
        self.client = client

    def connect(self, **_kwargs):
        return self.client


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    """App montada com store temporario + registry/resolver falsos injetados."""
    conn = service.open_store(str(tmp_path / "connector.db"))
    plugin_api.set_store(conn)
    plugin_api.set_secret_resolver(resolver)
    plugin_api.set_driver_registry(_registry())

    app = FastAPI()
    plugin_api.install_error_handlers(app)  # 4.7: 4xx/5xx sempre {error, detail}
    app.include_router(plugin_api.router, prefix=PREFIX)
    test_client = TestClient(app)
    yield test_client, conn
    # nao vazar estado entre testes
    plugin_api.set_store(None)
    plugin_api.set_secret_resolver(None)
    plugin_api.set_driver_registry(None)
    plugin_api.set_connection_change_emitter(None)
    conn.close()


def _registry(**overrides):
    from drivers import DriverRegistry

    drivers = {"sqlserver": _FakeSqlDriver(), "postgres": _FakeSqlDriver(), "redis": _FakeRedisDriver()}
    drivers.update(overrides)
    return DriverRegistry(drivers)


def _seed_connection(conn, connection_id="prod-sqlserver", engine="sqlserver", **overrides):
    """Cria a conexao dona das linhas do log (FK ligada -> cascata verificada)."""
    return service.upsert_connection(conn, _connection(id=connection_id, engine=engine, **overrides))


def _seed_queries(conn, rows):
    """Insere linhas no query_log na ordem informada (para /queries e /status)."""
    for index, row in enumerate(rows):
        service.append_query_log(
            conn,
            connection_id=row.get("connection_id", "prod-sqlserver"),
            engine=row.get("engine", "sqlserver"),
            environment=row.get("environment", "prod"),
            statement=row.get("statement", f"SELECT {index}"),
            source=row.get("source", "ui"),
            status=row.get("status", "ok"),
            duration_ms=row.get("duration_ms", index),
            session_id=row.get("session_id"),
            ts=row["ts"],
        )


def _timestamps(count):
    from datetime import datetime, timedelta, timezone

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [
        (base + timedelta(seconds=offset)).isoformat(timespec="seconds").replace("+00:00", "Z")
        for offset in range(count)
    ]


# --------------------------------------------------------------------------
# 4.1 CRUD de conexões
# --------------------------------------------------------------------------


def test_post_creates_connection_and_it_shows_up_in_the_list(client):
    test_client, _ = client

    created = test_client.post(f"{PREFIX}/connections", json=_connection())

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["ok"] is True
    assert body["connection"]["id"] == "prod-sqlserver"

    listed = test_client.get(f"{PREFIX}/connections").json()

    assert [row["id"] for row in listed["connections"]] == ["prod-sqlserver"]
    # defaults aplicados
    row = listed["connections"][0]
    assert row["read_only"] == 1 and row["enabled"] == 1
    assert row["options_json"] == "{}"


def test_duplicate_id_returns_409_and_keeps_the_existing_row(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201

    conflict = test_client.post(
        f"{PREFIX}/connections", json=_connection(label="Outra label", host="outro.host")
    )

    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert set(body) == {"error", "detail"} and body["error"] == "connection_exists"
    # a existente nao foi alterada
    assert service.get_connection(conn, "prod-sqlserver")["label"] == "Produção - SQL Server"


def test_put_upserts_and_keeps_defaults(client):
    test_client, conn = client

    updated = test_client.put(
        f"{PREFIX}/connections/prod-sqlserver",
        json=_connection(label="Prod SQL", host="sql2.prod.local", read_only=0),
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["connection"]["host"] == "sql2.prod.local"
    assert service.get_connection(conn, "prod-sqlserver")["read_only"] == 0

    # upsert puro: PUT tambem cria, e o id do path sempre vence
    created = test_client.put(
        f"{PREFIX}/connections/cache-local",
        json=_connection(id="ignorado", engine="redis", host="cache.local", port=6379),
    )
    assert created.status_code == 200
    assert created.json()["connection"]["id"] == "cache-local"
    assert created.json()["connection"]["read_only"] == 1  # default
    assert {row["id"] for row in service.list_connections(conn)} == {
        "prod-sqlserver",
        "cache-local",
    }


def test_delete_removes_the_connection_and_its_cascaded_log(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201
    service.append_query_log(
        conn,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement="SELECT 1",
        source="ui",
    )

    deleted = test_client.delete(f"{PREFIX}/connections/prod-sqlserver")

    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    assert service.get_connection(conn, "prod-sqlserver") is None
    # cascata: o log da conexao sumiu do GET /queries
    assert test_client.get(f"{PREFIX}/queries").json()["queries"] == []
    assert test_client.delete(f"{PREFIX}/connections/prod-sqlserver").status_code == 404


def test_no_response_ever_carries_the_secret_value(client):
    test_client, _ = client
    payload = _connection()

    responses = [
        test_client.post(f"{PREFIX}/connections", json=payload),
        test_client.get(f"{PREFIX}/connections"),
        test_client.put(f"{PREFIX}/connections/prod-sqlserver", json=payload),
        test_client.post(f"{PREFIX}/connections/prod-sqlserver/test"),
    ]

    for response in responses:
        assert SECRET_VALUE not in response.text, response.url
        assert "Traceback" not in response.text

    row = test_client.get(f"{PREFIX}/connections").json()["connections"][0]
    assert row["secret_ref"] == "DB_PROD_SQLSERVER_PASSWORD"
    assert row["secret_state"] == service.STATE_RESOLVED
    # nenhuma chave que possa conter valor, so a referencia e o estado
    assert not {"password", "secret", "secret_value"} & set(row)


def test_invalid_body_returns_4xx_with_error_and_detail(client):
    test_client, conn = client

    response = test_client.post(f"{PREFIX}/connections", json=_connection(engine="oracle"))

    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == {"error", "detail"}
    assert "sqlserver" in body["detail"] and "Traceback" not in response.text
    assert service.list_connections(conn) == []


# --------------------------------------------------------------------------
# 4.2 teste de conexão
# --------------------------------------------------------------------------


def test_successful_test_logs_ui_row_and_appears_in_queries(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201

    response = test_client.post(f"{PREFIX}/connections/prod-sqlserver/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and isinstance(body["latency_ms"], int)
    assert body["server_version"] == "16.0.1000"

    queries = test_client.get(f"{PREFIX}/queries").json()["queries"]
    assert len(queries) == 1
    row = queries[0]
    assert row["source"] == "ui"
    assert row["statement"] is None
    assert row["status"] == "ok"
    assert row["connection_id"] == "prod-sqlserver"
    assert row["duration_ms"] == body["latency_ms"]


def test_driver_error_never_carries_the_dsn_to_the_response_or_the_log(client):
    """Driver que ecoa o DSN: nem a resposta nem o query_log guardam o DSN.

    Spec backend-api/connection-store proibem o DSN completo (ele carrega
    usuario, senha e topologia), no corpo da resposta **e** no log servido ao
    Desktop por ``GET /queries``.
    """
    test_client, conn = client
    dsn = "postgres://hermes_ro:s3nh4SuperSecreta@db.prod.local:5432/HubFinanceira"
    assert (
        test_client.post(
            f"{PREFIX}/connections", json=_connection(id="pg-dsn", engine="postgres")
        ).status_code
        == 201
    )
    plugin_api.set_driver_registry(
        _registry(postgres=_FakeSqlDriver(raises=RuntimeError(f"could not connect to {dsn}")))
    )

    response = test_client.post(f"{PREFIX}/connections/pg-dsn/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert "postgres://" not in response.text
    assert "s3nh4SuperSecreta" not in response.text
    assert "db.prod.local" not in response.text
    assert "[dsn omitido]" in body["error"]

    row = test_client.get(f"{PREFIX}/queries").json()["queries"][0]
    assert row["status"] == "error"
    assert "postgres://" not in (row["error"] or "")
    assert "s3nh4SuperSecreta" not in (row["error"] or "")
    assert "db.prod.local" not in (row["error"] or "")
    assert "[dsn omitido]" in row["error"]


def test_failed_test_is_logged_with_error_status(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201
    plugin_api.set_driver_registry(
        _registry(sqlserver=_FakeSqlDriver(raises=RuntimeError(f"login failed senha {SECRET_VALUE}")))
    )

    response = test_client.post(f"{PREFIX}/connections/prod-sqlserver/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["error"]
    assert SECRET_VALUE not in response.text and "Traceback" not in response.text

    row = test_client.get(f"{PREFIX}/queries").json()["queries"][0]
    assert row["status"] == "error"
    assert row["source"] == "ui" and row["statement"] is None


def test_testing_an_unknown_connection_returns_404_without_stack_trace(client):
    test_client, _ = client

    response = test_client.post(f"{PREFIX}/connections/nao-existe/test")

    assert response.status_code == 404, response.text
    body = response.json()
    assert set(body) == {"error", "detail"}
    assert body["error"] == "connection_not_found"
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------
# 4.3 GET /queries — ordem decrescente, limit, connection_id, since_id
# --------------------------------------------------------------------------


def test_queries_are_newest_first(client):
    test_client, conn = client
    _seed_connection(conn)
    timestamps = _timestamps(3)
    _seed_queries(conn, [{"ts": ts} for ts in timestamps])

    rows = test_client.get(f"{PREFIX}/queries").json()["queries"]

    assert [row["ts"] for row in rows] == list(reversed(timestamps))


def test_queries_respect_limit_connection_id_and_since_id(client):
    test_client, conn = client
    _seed_connection(conn)
    _seed_connection(conn, connection_id="cache-local", engine="redis")
    timestamps = _timestamps(4)
    _seed_queries(
        conn,
        [
            {"ts": timestamps[0], "connection_id": "prod-sqlserver"},
            {"ts": timestamps[1], "connection_id": "cache-local", "engine": "redis"},
            {"ts": timestamps[2], "connection_id": "prod-sqlserver"},
            {"ts": timestamps[3], "connection_id": "cache-local", "engine": "redis"},
        ],
    )

    limited = test_client.get(f"{PREFIX}/queries?limit=2").json()["queries"]
    assert len(limited) == 2
    assert limited[0]["ts"] == timestamps[3]

    by_connection = test_client.get(f"{PREFIX}/queries?connection_id=cache-local").json()
    assert {row["connection_id"] for row in by_connection["queries"]} == {"cache-local"}
    assert len(by_connection["queries"]) == 2

    since = test_client.get(f"{PREFIX}/queries?since_id=2").json()["queries"]
    assert [row["id"] for row in since] == [4, 3]

    combined = test_client.get(
        f"{PREFIX}/queries?connection_id=prod-sqlserver&since_id=1&limit=10"
    ).json()
    assert [row["connection_id"] for row in combined["queries"]] == ["prod-sqlserver"]
    assert len(combined["queries"]) == 1


def test_queries_expose_metadata_and_no_secret_or_dsn(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201
    _seed_queries(conn, [{"ts": _timestamps(1)[0], "statement": "SELECT * FROM t WHERE id = 42"}])

    response = test_client.get(f"{PREFIX}/queries")

    assert SECRET_VALUE not in response.text
    row = response.json()["queries"][0]
    assert {"connection_id", "engine", "environment", "duration_ms", "status"} <= set(row)
    assert "42" not in row["statement"]  # literal mascarado


# --------------------------------------------------------------------------
# 4.4 GET /status
# --------------------------------------------------------------------------


def test_status_lists_running_query_and_last_seq(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201
    service.start_query_log(
        conn,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement="SELECT 1",
        source="ui",
        session_id="sess-a",
    )
    _seed_queries(conn, [{"ts": _timestamps(1)[0], "status": "ok"}])

    body = test_client.get(f"{PREFIX}/status").json()

    assert body["active"]
    active = body["active"][0]
    assert active["status"] == "running"
    assert active["connection_id"] == "prod-sqlserver"
    assert active["engine"] == "sqlserver"
    assert active["ts"]
    assert body["last_seq"] == service.last_query_log_id(conn) == 2
    assert body["connections"] == 1
    assert body["counters"]["running"] == 1 and body["counters"]["ok"] == 1


def test_status_session_id_filters_active_and_leaves_last_seq_global(client):
    test_client, conn = client
    _seed_connection(conn)
    for session in ("sess-a", "sess-b"):
        service.start_query_log(
            conn,
            connection_id="prod-sqlserver",
            engine="sqlserver",
            environment="prod",
            statement=None,
            source="ui",
            session_id=session,
        )

    filtered = test_client.get(f"{PREFIX}/status?session_id=sess-b").json()
    unfiltered = test_client.get(f"{PREFIX}/status").json()

    assert [row["session_id"] for row in filtered["active"]] == ["sess-b"]
    assert len(unfiltered["active"]) == 2
    assert filtered["last_seq"] == unfiltered["last_seq"] == 2


def test_active_leaves_status_once_the_query_finishes(client):
    test_client, conn = client
    _seed_connection(conn)
    query_id = service.start_query_log(
        conn,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement=None,
        source="ui",
        session_id="sess-a",
    )

    assert len(test_client.get(f"{PREFIX}/status").json()["active"]) == 1

    service.finish_query_log(conn, query_id, status="ok", rows=3, duration_ms=7)

    body = test_client.get(f"{PREFIX}/status").json()
    assert body["active"] == []  # reconciliacao apos evento perdido
    assert body["counters"]["ok"] == 1 and body["counters"]["running"] == 0


def test_status_route_never_leaks_a_secret(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201
    service.start_query_log(
        conn,
        connection_id="prod-sqlserver",
        engine="sqlserver",
        environment="prod",
        statement=None,
        source="ui",
    )

    response = test_client.get(f"{PREFIX}/status")

    assert SECRET_VALUE not in response.text
    assert json.dumps(response.json(), ensure_ascii=False)


# --------------------------------------------------------------------------
# 4.6 evento connection.changed (emissao real: broadcast_plugin_event do host)
# --------------------------------------------------------------------------


class _Emitter:
    """Listener injetado: grava (plugin_id, event, payload) e o estado do store."""

    def __init__(self, conn=None, raises=None):
        self.conn = conn
        self.raises = raises
        self.events: list[tuple[str, str, dict]] = []

    def __call__(self, plugin_id, event, payload):
        self.events.append((plugin_id, event, dict(payload)))
        if self.raises:
            raise self.raises


def test_crud_emits_connection_changed_with_id_and_action(client):
    test_client, conn = client
    emitter = _Emitter(conn)
    plugin_api.set_connection_change_emitter(emitter)

    created = test_client.post(f"{PREFIX}/connections", json=_connection())
    updated = test_client.put(
        f"{PREFIX}/connections/prod-sqlserver", json=_connection(label="Prod SQL")
    )
    deleted = test_client.delete(f"{PREFIX}/connections/prod-sqlserver")

    assert created.status_code == 201 and updated.status_code == 200
    assert deleted.status_code == 200
    assert emitter.events == [
        ("database-connector", "connection.changed",
         {"connection_id": "prod-sqlserver", "action": "created"}),
        ("database-connector", "connection.changed",
         {"connection_id": "prod-sqlserver", "action": "updated"}),
        ("database-connector", "connection.changed",
         {"connection_id": "prod-sqlserver", "action": "deleted"}),
    ]


def test_connection_changed_is_emitted_only_after_the_operation_succeeded(client):
    """created ve a conexao persistida; conflito (409) nao emite nada."""
    test_client, conn = client
    seen_after_create: list[bool] = []

    class Recorder(_Emitter):
        def __call__(self, plugin_id, event, payload):
            seen_after_create.append(service.get_connection(conn, payload["connection_id"]) is not None)
            return super().__call__(plugin_id, event, payload)

    plugin_api.set_connection_change_emitter(Recorder(conn))

    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201
    conflict = test_client.post(f"{PREFIX}/connections", json=_connection(label="Outra"))

    assert conflict.status_code == 409
    assert seen_after_create == [True]  # emitido depois do sucesso
    plugin_api.get_connection_change_emitter()
    assert len(plugin_api.get_connection_change_emitter().events) == 1  # so o create


def test_failing_emitter_never_breaks_the_crud_response(client):
    """Emissao fora do ar nao pode derrubar 201/200 - REST e a fonte de verdade."""
    test_client, conn = client
    plugin_api.set_connection_change_emitter(
        _Emitter(conn, raises=RuntimeError("broadcast fora do ar"))
    )

    created = test_client.post(f"{PREFIX}/connections", json=_connection())
    updated = test_client.put(f"{PREFIX}/connections/prod-sqlserver", json=_connection(label="x"))
    deleted = test_client.delete(f"{PREFIX}/connections/prod-sqlserver")

    assert created.status_code == 201, created.text
    assert updated.status_code == 200, updated.text
    assert deleted.status_code == 200, deleted.text
    assert "Traceback" not in deleted.text
    assert service.get_connection(conn, "prod-sqlserver") is None


def test_emitter_is_resolved_lazily_from_the_host_module(client, monkeypatch):
    """Sem emissor injetado cai no ``broadcast_plugin_event`` do host (lazy)."""
    import sys
    import types

    test_client, _ = client
    seen: list[tuple] = []
    module_name, attribute = plugin_api.BROADCAST_SOURCES[0]
    fake_host = types.ModuleType(module_name)
    setattr(fake_host, attribute, lambda *args: seen.append(args))
    monkeypatch.setitem(sys.modules, module_name, fake_host)

    response = test_client.post(f"{PREFIX}/connections", json=_connection())

    assert response.status_code == 201, response.text
    assert seen == [
        (
            "database-connector",
            "connection.changed",
            {"connection_id": "prod-sqlserver", "action": "created"},
        )
    ]


# --------------------------------------------------------------------------
# 4.7 erros: { error, detail } em texto claro, sem traceback/DSN/segredo
# --------------------------------------------------------------------------


def test_invalid_body_returns_error_and_detail_on_every_write_route(client):
    test_client, conn = client
    assert test_client.post(f"{PREFIX}/connections", json=_connection()).status_code == 201

    bad_bodies = [
        ("post", f"{PREFIX}/connections", {"json": ["nao-e-objeto"]}),
        ("post", f"{PREFIX}/connections", {"json": "texto"}),
        ("post", f"{PREFIX}/connections", {"json": {"engine": "oracle", "id": "x"}}),
        ("put", f"{PREFIX}/connections/prod-sqlserver", {"json": ["nao-e-objeto"]}),
        ("put", f"{PREFIX}/connections/prod-sqlserver", {"json": "texto"}),
    ]

    for method, url, kwargs in bad_bodies:
        response = getattr(test_client, method)(url, **kwargs)
        assert response.status_code // 100 == 4, (url, response.text)
        body = response.json()
        assert set(body) == {"error", "detail"}, (url, body)
        assert isinstance(body["detail"], str) and body["detail"]
        assert "Traceback" not in response.text
        assert SECRET_VALUE not in response.text


def test_unknown_connection_returns_404_with_error_and_detail(client):
    test_client, _ = client

    deleted = test_client.delete(f"{PREFIX}/connections/nao-existe")
    tested = test_client.post(f"{PREFIX}/connections/nao-existe/test")

    for response in (deleted, tested):
        assert response.status_code == 404, response.text
        body = response.json()
        assert set(body) == {"error", "detail"}
        assert body["error"] == "connection_not_found"
        assert "nao existe" in body["detail"]
        assert "Traceback" not in response.text


def test_put_on_unknown_id_upserts_and_the_row_shows_up_in_the_list(client):
    """Contrato aceito em 4.1/spec: PUT e update **e** upsert (nao da 404)."""
    test_client, _ = client

    response = test_client.put(
        f"{PREFIX}/connections/cache-local",
        json=_connection(id="ignorado", engine="redis", host="cache.local", port=6379),
    )

    assert response.status_code == 200, response.text
    assert response.json()["connection"]["id"] == "cache-local"
    listed = test_client.get(f"{PREFIX}/connections").json()["connections"]
    assert [row["id"] for row in listed] == ["cache-local"]


def test_unexpected_failure_inside_a_route_becomes_500_without_traceback(client, monkeypatch):
    test_client, _ = client

    def boom(_conn):
        raise RuntimeError(f"disco cheio: dsn=postgres://u:{SECRET_VALUE}@h/db")

    monkeypatch.setattr(service, "list_connections", boom)

    response = test_client.get(f"{PREFIX}/connections")

    assert response.status_code == 500, response.text
    body = response.json()
    assert set(body) == {"error", "detail"} and body["error"] == "internal_error"
    assert "RuntimeError" in body["detail"]  # acionavel para quem opera
    assert "Traceback" not in response.text
    assert SECRET_VALUE not in response.text and "postgres://" not in response.text


def test_app_error_handler_covers_routes_outside_the_router():
    """Handler generico do app: qualquer excecao vira 500 {error, detail}."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TestClient

    app = FastAPI()
    plugin_api.install_error_handlers(app)

    @app.get("/fora-do-router")
    def explode():
        raise RuntimeError("boom interno")

    response = _TestClient(app, raise_server_exceptions=False).get("/fora-do-router")

    assert response.status_code == 500, response.text
    body = response.json()
    assert set(body) == {"error", "detail"} and body["error"] == "internal_error"
    assert "Traceback" not in response.text and "boom" not in response.text


def test_query_and_path_errors_also_come_as_error_and_detail(client):
    test_client, _ = client

    invalid_limit = test_client.get(f"{PREFIX}/queries?limit=abc")
    unknown_route = test_client.get(f"{PREFIX}/nao-existe")

    for response, expected_status in ((invalid_limit, 400), (unknown_route, 404)):
        assert response.status_code == expected_status, response.text
        body = response.json()
        assert set(body) == {"error", "detail"}, (expected_status, body)
        assert isinstance(body["detail"], str) and body["detail"]
        assert "Traceback" not in response.text


# --------------------------------------------------------------------------
# correcoes pos-revisao: probe apos close e 4xx/5xx sem handler de app
# --------------------------------------------------------------------------


def test_probe_ok_is_reported_and_logged_when_the_client_is_already_closed(client):
    """Versao capturada **antes** do close: probe ok responde 200 e loga a linha."""
    test_client, conn = client
    connection_id = "prod-pg"
    assert (
        test_client.post(f"{PREFIX}/connections", json=_connection(id=connection_id, engine="postgres")).status_code
        == 201
    )

    fake_client = _PgLikeClient(version="16.2")
    plugin_api.set_driver_registry(_registry(postgres=_FixedClientDriver(fake_client)))

    response = test_client.post(f"{PREFIX}/connections/{connection_id}/test")

    assert fake_client.closed is True  # o client foi fechado
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["server_version"] == "16.2"  # lido enquanto aberto
    assert "Traceback" not in response.text

    rows = service.list_query_log(conn, connection_id=connection_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "ui"
    assert rows[0]["status"] == "ok"
    assert rows[0]["statement"] is None
    assert rows[0]["duration_ms"] == body["latency_ms"]


def test_every_failure_is_error_and_detail_without_any_app_handler(tmp_path, monkeypatch):
    """O router se garante sozinho — o host Hermes monta o router, nao um app nosso."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TestClient

    conn = service.open_store(str(tmp_path / "connector.db"))
    plugin_api.set_store(conn)
    try:
        app = FastAPI()  # SEM install_error_handlers: so o router do plugin
        app.include_router(plugin_api.router, prefix=PREFIX)
        test_client = _TestClient(app)  # excecao escapada viraria erro do teste

        cases = [
            (test_client.get(f"{PREFIX}/nao-existe"), 404),
            (test_client.delete(f"{PREFIX}/ping"), 405),
            (test_client.get(f"{PREFIX}/queries?limit=abc"), 400),
            (test_client.get(f"{PREFIX}/status?limit=0"), 400),
            # JSON malformado: antes virava o 422 do framework (ecoando input)
            (
                test_client.post(
                    f"{PREFIX}/connections",
                    content=b"{nao-e-json",
                    headers={"content-type": "application/json"},
                ),
                400,
            ),
            (
                test_client.put(
                    f"{PREFIX}/connections/x",
                    content=b"[",
                    headers={"content-type": "application/json"},
                ),
                400,
            ),
        ]
        for response, expected_status in cases:
            assert response.status_code == expected_status, (expected_status, response.text)
            body = response.json()
            assert set(body) == {"error", "detail"}, (expected_status, body)
            assert isinstance(body["detail"], str) and body["detail"]
            assert "Traceback" not in response.text

        def boom(*_args, **_kwargs):
            raise RuntimeError(f"disco cheio: dsn=postgres://u:{SECRET_VALUE}@h/db")

        monkeypatch.setattr(service, "status_snapshot", boom)

        response = test_client.get(f"{PREFIX}/status")

        assert response.status_code == 500, response.text
        body = response.json()
        assert set(body) == {"error", "detail"} and body["error"] == "internal_error"
        assert "RuntimeError" in body["detail"]  # acionavel, sem vazar a causa
        assert "Traceback" not in response.text
        assert SECRET_VALUE not in response.text and "postgres://" not in response.text
    finally:
        plugin_api.set_store(None)
        conn.close()


def test_contract_holds_for_unusual_verbs_trailing_slash_and_head(tmp_path):
    """TRACE, barra final e HEAD tambem respeitam {error, detail}/o recurso.

    Sem handler de app (o host monta o router, nao um app nosso): o formato
    tem que vir do proprio plugin.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TestClient

    conn = service.open_store(str(tmp_path / "connector.db"))
    plugin_api.set_store(conn)
    try:
        app = FastAPI()  # SEM install_error_handlers
        app.include_router(plugin_api.router, prefix=PREFIX)
        test_client = _TestClient(app)

        # verbo fora do esperado: o catch-all responde, nao o framework
        trace = test_client.request("TRACE", f"{PREFIX}/connections")
        assert trace.status_code == 405, (trace.status_code, trace.text)
        assert set(trace.json()) == {"error", "detail"}
        assert trace.json()["error"] == "method_not_allowed"

        # barra final: o mesmo recurso (307 para o canonical, ou 200 direto)
        slashed = test_client.get(f"{PREFIX}/connections/", follow_redirects=False)
        assert slashed.status_code in (200, 307), (slashed.status_code, slashed.text)
        if slashed.status_code == 307:
            assert slashed.headers["location"].endswith("/connections")
        assert test_client.get(f"{PREFIX}/connections/").status_code == 200

        # HEAD: HTTP nao entrega corpo, entao o contrato vem no status + Allow
        head = test_client.head(f"{PREFIX}/connections")
        assert head.status_code == 405
        assert {"GET", "POST"} <= set(head.headers["allow"].split(", "))
    finally:
        plugin_api.set_store(None)
        conn.close()
