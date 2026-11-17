"""Tasks 2.1/2.2/2.3 — ciclo único ``service.run_query``: log, eventos, códigos
e redaction.

Every driver is a **fake** injected into the cycle: no database is contacted
(the real instances are still pending — host ``hostgator``). What is exercised
here is the cycle itself: one line per query, the same ``query_id`` in every
event, the stable ``code`` vocabulary and the fact that neither the log nor the
events ever carry a secret, a DSN, the params or the result.

Eventos reais via ``broadcast_plugin_event`` do host pendentes (hostgator): os
testes injetam o emissor, com o mesmo padrão do adaptador REST.
"""

import time

import pytest
from pathlib import Path

import service
from drivers import DriverMissingError, DriverRegistry
from drivers import redis as redis_driver


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _Recorder:
    """Emitter fake: every event received, in order."""

    def __init__(self, raises=None):
        self.events = []
        self.raises = raises

    def __call__(self, plugin_id, event, payload):
        if self.raises is not None:
            raise self.raises
        self.events.append((plugin_id, event, dict(payload)))
        return True

    def names(self):
        return [event for _plugin, event, _payload in self.events]

    def payload(self, event):
        for _plugin, name, payload in self.events:
            if name == event:
                return payload
        raise AssertionError(f"evento nao emitido: {event}")


class _FakeAdapter:
    """Uniform driver interface (``connect``/``execute``)."""

    def __init__(self, result=None, raises=None):
        self.result = result or {"rows": [], "row_count": 0, "duration_ms": 1}
        self.raises = raises
        self.calls = []
        self.clients = []

    def connect(self, row, secret_value=None):
        self.calls.append(("connect", dict(row), secret_value))
        client = ("client", len(self.clients))
        self.clients.append(client)
        return client

    def execute(self, client, statement, *rest, **kwargs):
        self.calls.append(("execute", client, statement, rest, kwargs))
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


class _FakeMssqlCursor:
    def __init__(self, client):
        self.client = client

    def execute(self, statement):
        self.client.executed.append(statement)

    def fetchmany(self, size):
        got = self.client.rows[:size]
        self.rowcount = len(self.client.rows)
        return got

    def fetchall(self):
        return self.client.rows

    def close(self):
        self.client.cursors_closed += 1


class _FakeMssqlClient:
    def __init__(self, rows=()):
        self.rows = [list(row) for row in rows]
        self.executed = []
        self.rollbacks = 0
        self.cursors_closed = 0
        self.server_version = "16.0"

    def cursor(self):
        return _FakeMssqlCursor(self)

    def rollback(self):
        self.rollbacks += 1


class _FakeMssqlLib:
    """Fake pymssql: só ``connect(**kwargs)``."""

    def __init__(self, client):
        self.client = client

    def connect(self, **kwargs):
        self.kwargs = kwargs
        return self.client


class _ClosingClient:
    """Client that records whether it was released (P2b)."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _SlowConnectAdapter(_FakeAdapter):
    """Uniform driver whose ``connect`` takes longer than the whole budget."""

    def __init__(self, delay=0.3):
        super().__init__()
        self.delay = delay
        self.opened: list[_ClosingClient] = []

    def connect(self, row, secret_value=None):
        time.sleep(self.delay)
        client = _ClosingClient()
        self.opened.append(client)
        return client


class _Timeout(Exception):
    """Driver-specific timeout (redis/psycopg names carry ``Timeout``)."""


class _UnscopedSecretError(Exception):
    pass


class _SecretResolver:
    def __init__(self, mapping=None, raises=None):
        self.mapping = mapping or {}
        self.raises = raises

    def __call__(self, name):
        if self.raises is not None:
            raise self.raises
        return self.mapping.get(name)


def _resolver():
    return _SecretResolver({"DB_PROD_SQLSERVER_PASSWORD": "senha-super-secreta"})


def _row(**overrides):
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
        "options": {"connect_timeout_s": 5, "statement_timeout_s": 20},
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "connector.db")
    conn = service.open_store(path)
    yield conn
    conn.close()


@pytest.fixture()
def stored(db):
    """Connection really in the store (``query_log`` has a FK to it)."""
    return service.upsert_connection(db, _row())


@pytest.fixture(autouse=True)
def _providers():
    """Nothing leaks between tests: tirado o getter, vale o default (true)."""
    service.set_query_event_emitter(None)
    service.set_config_getter(None)
    yield
    service.set_query_event_emitter(None)
    service.set_config_getter(None)


def _lines(db):
    return service.list_query_log(db)


# --------------------------------------------------------------------------
# 2.1 ciclo completo: uma linha e eventos com o mesmo query_id
# --------------------------------------------------------------------------


def test_ciclo_ok_uma_unica_linha_running_ok(db, stored):
    driver = _FakeAdapter(
        {"rows": [[1, "a"], [2, "b"]], "row_count": 2, "truncated": False, "duration_ms": 12}
    )
    emitter = _Recorder()

    result = service.run_query(
        db,
        stored,
        "SELECT id, nome FROM clientes WHERE id = 7",
        source=service.SOURCE_AGENT,
        session_id="sessao-1",
        resolver=_resolver(),
        emitter=emitter,
        driver=driver,
    )

    assert result["ok"] is True
    assert result["row_count"] == 2
    assert result["duration_ms"] == 12

    lines = _lines(db)
    assert len(lines) == 1  # running -> ok: nunca duas linhas por ciclo
    assert lines[0]["query_id"] == result["query_id"]
    assert lines[0]["status"] == "ok"
    assert lines[0]["rows"] == 2
    assert lines[0]["source"] == "agent"
    assert lines[0]["session_id"] == "sessao-1"

    assert emitter.names() == ["query.started", "query.finished"]
    assert emitter.events[0][0] == "database-connector"

    started = emitter.payload("query.started")
    finished = emitter.payload("query.finished")
    assert started["query_id"] == result["query_id"] == finished["query_id"]
    for payload in (started, finished):
        assert payload["connection_id"] == "prod-sqlserver"
        assert payload["label"] == "Produção - SQL Server"
        assert payload["engine"] == "sqlserver"
        assert payload["environment"] == "prod"
        assert payload["database"] == "HubFinanceira"  # exibicao, nunca DSN
        assert payload["session_id"] == "sessao-1"
        assert payload["started_at"]
        # nem a statement: eventos carregam metadados, não o texto da consulta
        assert "statement" not in payload
    assert started["status"] == "running"
    assert finished["status"] == "ok"
    assert finished["rows"] == 2
    assert finished["duration_ms"] == 12
    assert finished["finished_at"]

    # segredo, DSN e resultado não aparecem em lugar nenhum
    for payload in (started, finished):
        assert "senha-super-secreta" not in str(payload)
        assert "sql.prod.local" not in str(payload)


def test_falha_no_driver_gera_started_failed_e_linha_error(db, stored):
    driver = _FakeAdapter(
        raises=RuntimeError(
            "conexao recusada em postgres://hermes_ro:senha-super-secreta@sql.prod.local/hub"
        )
    )
    emitter = _Recorder()

    result = service.run_query(
        db, stored, "SELECT 1", resolver=_resolver(), emitter=emitter, driver=driver
    )

    assert result["ok"] is False
    assert result["code"] == "db_error"
    assert emitter.names() == ["query.started", "query.failed"]

    failed = emitter.payload("query.failed")
    assert failed["status"] == "error"
    assert failed["query_id"] == result["query_id"]
    assert failed["error"] == result["error"]

    line = _lines(db)[0]
    assert len(_lines(db)) == 1
    assert line["query_id"] == result["query_id"]
    assert line["status"] == "error"
    assert line["error"] == result["error"]
    assert len(line["error"]) <= service.ERROR_MAX_CHARS
    assert "senha-super-secreta" not in line["error"]


def test_ciclo_repassa_statement_e_max_rows_com_teto(db, stored):
    driver = _FakeAdapter({"rows": [], "row_count": 0})

    service.run_query(
        db, stored, "SELECT * FROM tabela", 5000, resolver=_resolver(), driver=driver
    )

    call = [item for item in driver.calls if item[0] == "execute"][0]
    assert call[2] == "SELECT * FROM tabela"
    assert call[3][0] == service.MAX_ROWS_CAP  # teto de 1000
    assert call[4]["statement_timeout_s"] == 20  # statement_timeout da conexão


def test_abertura_recebe_o_segredo_resolvido_em_memoria(db, stored):
    driver = _FakeAdapter({"rows": [], "row_count": 0})

    service.run_query(db, stored, "SELECT 1", resolver=_resolver(), driver=driver)

    call = [item for item in driver.calls if item[0] == "connect"][0]
    assert call[2] == "senha-super-secreta"  # valor em memoria, nunca no log


def test_linha_e_eventos_usam_max_rows_do_padrao_quando_ausente(db, stored):
    driver = _FakeAdapter({"rows": [[1]], "row_count": 1, "truncated": True})

    result = service.run_query(db, stored, "SELECT 1", resolver=_resolver(), driver=driver)

    call = [item for item in driver.calls if item[0] == "execute"][0]
    assert call[3][0] == service.DEFAULT_MAX_ROWS
    assert result["truncated"] is True


# --------------------------------------------------------------------------
# 2.2 mapping driver -> code estável
# --------------------------------------------------------------------------


def test_mapping_por_classe_de_excecao():
    assert service.classify_driver_error(TimeoutError("estourou")) == "timeout"
    assert service.classify_driver_error(_Timeout("read timed out")) == "timeout"
    assert service.classify_driver_error(RuntimeError("boom")) == "db_error"
    assert (
        service.classify_driver_error(
            DriverMissingError("redis", "redis", "uv pip install redis")
        )
        == "driver_missing"
    )


def test_mapping_respeita_code_declarado_na_excecao():
    class _ReadOnly(Exception):
        code = "read_only"

    class _NotFound(Exception):
        code = "not_found"

    assert service.classify_driver_error(_ReadOnly()) == "read_only"
    assert service.classify_driver_error(_NotFound()) == "not_found"


def test_mensagem_do_mapping_e_truncada_sem_segredo_nem_host():
    exc = RuntimeError(
        "falha em postgres://hermes_ro:senha-super-secreta@sql.prod.local/hub " + "x" * 2000
    )
    code, message = service.map_driver_error(
        exc, secrets=["senha-super-secreta"], hosts=["sql.prod.local"]
    )

    assert code == "db_error"
    assert len(message) <= service.ERROR_MAX_CHARS
    assert "senha-super-secreta" not in message
    assert "sql.prod.local" not in message
    assert "postgres://" not in message


def test_timeout_no_ciclo_mapeia_para_timeout(db, stored):
    driver = _FakeAdapter(raises=_Timeout("read timed out"))
    emitter = _Recorder()

    result = service.run_query(
        db, stored, "SELECT 1", resolver=_resolver(), emitter=emitter, driver=driver
    )

    assert result["code"] == "timeout"
    assert emitter.names() == ["query.started", "query.failed"]
    assert _lines(db)[0]["status"] == "error"


def _wait_until(condition, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_run_with_deadline_fecha_o_valor_tardio(db):
    """P2b: o que a thread abandonada produz depois do prazo é liberado."""
    holder: dict[str, _ClosingClient] = {}

    def slow() -> _ClosingClient:
        time.sleep(0.3)
        holder["client"] = _ClosingClient()
        return holder["client"]

    value, exc = service._run_with_deadline(
        0.05, slow, on_late_value=service._close_quietly
    )

    assert isinstance(exc, TimeoutError)
    assert value is None
    assert _wait_until(lambda: "client" in holder)
    assert _wait_until(lambda: holder["client"].closed) is True


def test_connect_tardio_e_fechado_quando_o_prazo_estoura(db):
    """P2b: no ciclo, o cliente do connect que completa depois é fechado."""
    row = service.upsert_connection(
        db,
        _row(id="slow-sql", options={"connect_timeout_s": 0.05, "statement_timeout_s": 20}),
    )
    adapter = _SlowConnectAdapter()

    result = service.run_query(db, row, "SELECT 1", resolver=_resolver(), driver=adapter)

    assert result["code"] == "timeout"
    assert _lines(db)[0]["status"] == "error"
    # a thread abandonada conclui o connect depois: o cliente não pode vazar
    assert _wait_until(lambda: bool(adapter.opened))
    assert _wait_until(lambda: adapter.opened[0].closed) is True


def test_segredo_missing_acontece_antes_do_started(db, stored):
    """Sem linha 'running' orfa e sem evento: falha no pre-flight."""
    driver = _FakeAdapter()
    emitter = _Recorder()

    result = service.run_query(
        db,
        stored,
        "SELECT 1",
        resolver=_SecretResolver({}),  # env ausente -> missing
        emitter=emitter,
        driver=driver,
    )

    assert result["ok"] is False
    assert result["code"] == "secret_missing"
    assert result["query_id"] is None
    assert emitter.events == []  # sem started: não abriu conexão
    assert _lines(db) == []  # sem linha orfa
    assert driver.calls == []


def test_segredo_unscoped_tambem_e_secret_missing(db, stored):
    emitter = _Recorder()

    result = service.run_query(
        db,
        stored,
        "SELECT 1",
        resolver=_SecretResolver(raises=_UnscopedSecretError("no secret scope")),
        emitter=emitter,
        driver=_FakeAdapter(),
    )

    assert result["code"] == "secret_missing"
    assert emitter.events == []
    assert _lines(db) == []


def test_driver_missing_no_pre_flight_traz_install(db, stored):
    emitter = _Recorder()

    result = service.run_query(
        db,
        stored,
        "SELECT 1",
        registry=DriverRegistry(overrides={"sqlserver": None}),
        resolver=_resolver(),
        emitter=emitter,
    )

    assert result["code"] == "driver_missing"
    assert result["install"]
    assert "pymssql" in result["error"] + result["install"]
    assert emitter.events == []
    assert _lines(db) == []


def test_conexao_nao_informada_e_not_found(db):
    result = service.run_query(db, {}, "SELECT 1")

    assert result["ok"] is False
    assert result["code"] == "not_found"
    assert _lines(db) == []


def test_emissor_que_falha_nao_derruba_o_ciclo(db, stored):
    driver = _FakeAdapter({"rows": [[1]], "row_count": 1, "duration_ms": 2})
    emitter = _Recorder(raises=RuntimeError("broadcast fora do ar"))

    result = service.run_query(
        db, stored, "SELECT 1", resolver=_resolver(), emitter=emitter, driver=driver
    )

    assert result["ok"] is True
    assert _lines(db)[0]["status"] == "ok"


# --------------------------------------------------------------------------
# 2.3 redaction no ponto único do ciclo
# --------------------------------------------------------------------------


def test_log_traz_statement_redigido(db, stored):
    statement = (
        "SELECT * FROM clientes /* comentario interno */ "
        "WHERE id = 42 AND nome = 'João' -- resto"
    )

    service.run_query(
        db,
        stored,
        statement,
        resolver=_resolver(),
        driver=_FakeAdapter({"rows": [], "row_count": 0}),
    )

    logged = _lines(db)[0]["statement"]
    assert len(logged) <= service.STATEMENT_MAX_CHARS
    assert "?" in logged  # literais mascarados
    assert "42" not in logged
    assert "João" not in logged
    assert "comentario interno" not in logged  # sem comentário
    assert "--" not in logged


def test_statement_longa_e_truncada_em_500(db, stored):
    service.run_query(
        db,
        stored,
        "SELECT " + "a, " * 1000 + "b FROM t",
        resolver=_resolver(),
        driver=_FakeAdapter({"rows": [], "row_count": 0}),
    )

    assert len(_lines(db)[0]["statement"]) <= service.STATEMENT_MAX_CHARS


def test_toggle_log_statement_desligado_grava_apenas_metadados(db, stored):
    service.set_config_getter(lambda key, default: False)

    service.run_query(
        db,
        stored,
        "SELECT sigilo FROM clientes",
        resolver=_resolver(),
        driver=_FakeAdapter({"rows": [], "row_count": 0}),
    )

    line = _lines(db)[0]
    assert line["statement"] is None
    assert line["connection_id"] == "prod-sqlserver"  # metadados seguem lá
    assert line["engine"] == "sqlserver"
    assert line["duration_ms"] is not None


def test_toggle_ligado_por_padrao_quando_nao_ha_getter(db, stored):
    service.run_query(
        db,
        stored,
        "SELECT 1",
        resolver=_resolver(),
        driver=_FakeAdapter({"rows": [], "row_count": 0}),
    )

    assert _lines(db)[0]["statement"] == "SELECT ?"


def test_toggle_pode_vir_de_getter_injetado_no_ciclo(db, stored):
    service.run_query(
        db,
        stored,
        "SELECT 1",
        resolver=_resolver(),
        driver=_FakeAdapter({"rows": [], "row_count": 0}),
        config_getter=lambda key, default: {"log_statement": False}.get(key, default),
    )

    assert _lines(db)[0]["statement"] is None


def test_erro_com_dsn_nao_vaza_no_log_nem_no_evento(db, stored):
    driver = _FakeAdapter(
        raises=RuntimeError("could not connect: postgres://u:segredo123@10.0.0.9/db")
    )
    emitter = _Recorder()

    result = service.run_query(
        db, stored, "SELECT 1", resolver=_resolver(), emitter=emitter, driver=driver
    )

    failed = emitter.payload("query.failed")
    for text in (result["error"], failed["error"], _lines(db)[0]["error"]):
        assert "segredo123" not in text
        assert "postgres://" not in text
        assert "[dsn omitido]" in text


def test_log_nunca_carrega_resultado_nem_segredo(db, stored):
    driver = _FakeAdapter({"rows": [["dado-sensivel"]], "row_count": 1, "duration_ms": 1})

    service.run_query(
        db,
        stored,
        "SELECT segredo FROM t",
        resolver=_resolver(),
        driver=driver,
        emitter=_Recorder(),
    )

    line = str(_lines(db)[0])
    assert "dado-sensivel" not in line
    assert "senha-super-secreta" not in line


# --------------------------------------------------------------------------
# redis no mesmo ciclo: comando + args e valor truncado em 64 KiB
# --------------------------------------------------------------------------


class _FakeRedisClient:
    def __init__(self, value="valor"):
        self.value = value
        self.calls = []
        self.pings = 0
        self.closed = False

    def execute_command(self, name, *args):
        self.calls.append((name, args))
        return self.value

    def ping(self):
        self.pings += 1
        return True

    def close(self):
        self.closed = True


class _FakeRedisLib:
    """Fake ``redis`` module (redis-py exposes the client as ``Redis``)."""

    def __init__(self, client):
        self.client = client
        self.kwargs = None

    def Redis(self, **kwargs):
        self.kwargs = kwargs
        return self.client


@pytest.fixture()
def redis_row(db):
    return service.upsert_connection(
        db,
        _row(id="cache", label="Cache", engine="redis", port=6379, database="2"),
    )


def test_ciclo_redis_usa_comando_e_args(db, redis_row):
    client = _FakeRedisClient("valor da chave")
    redis_driver.set_driver_module(_FakeRedisLib(client))
    try:
        result = service.run_query(
            db,
            redis_row,
            "GET",
            args=("minha:chave",),
            resolver=_resolver(),
            driver=redis_driver,
        )
    finally:
        redis_driver.forget_driver_module()

    assert result["ok"] is True
    assert result["value"] == "valor da chave"
    assert result["engine"] == "redis"
    assert client.calls == [("GET", ("minha:chave",))]
    assert _lines(db)[0]["status"] == "ok"


def test_driver_module_vem_do_registry_quando_nao_injetado():
    """Sem adapter injetado, o registry é o ponto único de import lazy (design 1)."""
    client = _FakeRedisClient("ok")
    library = _FakeRedisLib(client)

    try:
        module = service._driver_module(
            "redis", DriverRegistry(overrides={"redis": library})
        )
        assert module is redis_driver  # adapter uniforme da engine
        assert isinstance(redis_driver._driver(), _FakeRedisLib)  # lib do registry
    finally:
        redis_driver.forget_driver_module()


def test_ciclo_sqlserver_pelo_adapter_real_com_biblioteca_fake(db, stored):
    """Ciclo -> adapter real (READ ONLY) -> biblioteca fake: sem servidor."""
    from drivers import sqlserver as mssql

    fake = _FakeMssqlLib(_FakeMssqlClient([[1, "linha"]]))
    mssql.set_driver_module(fake)
    try:
        result = service.run_query(
            db,
            stored,
            "SELECT id, nome FROM clientes",
            registry=DriverRegistry(overrides={"sqlserver": fake}),
            resolver=_resolver(),
            emitter=_Recorder(),
        )
    finally:
        mssql.forget_driver_module()

    assert result["ok"] is True
    assert result["rows"] == [[1, "linha"]]
    # 1.5: T-SQL nao tem a declaracao — nunca enviada ao SQL Server
    assert mssql.READ_ONLY_STATEMENT not in fake.client.executed
    assert fake.client.rollbacks == 1  # transação nunca fica aberta
    assert _lines(db)[0]["status"] == "ok"


def test_resolve_broadcast_encontra_o_emissor_no_caminho_real_do_host(monkeypatch):
    """P1a: ``hermes_cli.plugin_events`` é o primeiro caminho tentado."""
    import sys
    import types

    assert service.BROADCAST_SOURCES[0] == (
        "hermes_cli.plugin_events",
        "broadcast_plugin_event",
    )
    seen: list[tuple] = []
    module_name, attribute = service.BROADCAST_SOURCES[0]
    fake_host = types.ModuleType(module_name)
    setattr(fake_host, attribute, lambda *args: seen.append(args))
    monkeypatch.setitem(sys.modules, module_name, fake_host)

    emitter = service.resolve_broadcast()

    assert callable(emitter)
    emitter("database-connector", "query.started", {"query_id": "q1"})
    assert seen == [("database-connector", "query.started", {"query_id": "q1"})]


def test_toggle_log_statement_esta_declarado_no_config_schema():
    """O toggle do ciclo é nativo do Hermes (settings do plugin), sem rota própria."""
    import yaml

    manifest_path = Path(__file__).resolve().parents[1] / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    field = manifest["config_schema"][service.CONFIG_LOG_STATEMENT]
    assert field["type"] == "bool"
    assert field["default"] is True


def test_ciclo_redis_trunca_valor_em_64kib(db, redis_row):
    big = "x" * (redis_driver.VALUE_MAX_BYTES + 2048)
    redis_driver.set_driver_module(_FakeRedisLib(_FakeRedisClient(big)))
    try:
        result = service.run_query(
            db,
            redis_row,
            "GET",
            args=("grande",),
            resolver=_resolver(),
            driver=redis_driver,
        )
    finally:
        redis_driver.forget_driver_module()

    assert result["truncated"] is True
    assert len(result["value"].encode("utf-8")) == redis_driver.VALUE_MAX_BYTES
    assert big not in str(_lines(db)[0])  # resultado nunca vai para o log
