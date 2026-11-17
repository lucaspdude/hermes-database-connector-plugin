"""Tasks 3.1–3.4 — tools do agente: wrapper à prova de exceção, ``check_fn``,
``db_list_connections``, ``db_query`` (política read-only) e ``db_redis_command``
(allowlist fechada).

Nada aqui fala com banco: os drivers são **fakes** injetados (Postgres/SQL Server
pelo parâmetro ``driver``, Redis pelo ``registry`` + ``drivers.redis``), e o
registro é exercitado por um ``ctx`` falso que captura as chamadas. Consultas
contra instâncias reais seguem pendentes (host ``hostgator``) e o **registro real
em ctx do Hermes também** (task 3.1).
"""

import json
import pathlib

import pytest
import yaml

import service
import tools
from drivers import DriverRegistry
from drivers import redis as redis_driver

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin.yaml"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _Recorder:
    """Emitter fake: todo evento recebido, em ordem."""

    def __init__(self):
        self.events = []

    def __call__(self, plugin_id, event, payload):
        self.events.append((plugin_id, event, dict(payload)))
        return True

    def names(self):
        return [event for _plugin, event, _payload in self.events]


class _FakeAdapter:
    """Interface uniforme do driver (``connect``/``execute``)."""

    def __init__(self, result=None, raises=None):
        self.result = result or {"rows": [[1, "a"]], "row_count": 1, "truncated": False, "duration_ms": 3}
        self.raises = raises
        self.calls = []

    def connect(self, row, secret_value=None):
        self.calls.append(("connect", dict(row), secret_value))
        return ("client", len(self.calls))

    def execute(self, client, statement, *rest, **kwargs):
        self.calls.append(("execute", client, statement, rest, kwargs))
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


class _FakeRedisClient:
    def __init__(self, value="valor"):
        self.value = value
        self.calls = []
        self.closed = False

    def execute_command(self, name, *args):
        self.calls.append((name, args))
        return self.value

    def ping(self):
        return True

    def close(self):
        self.closed = True


class _FakeRedisLib:
    """Fake do módulo ``redis`` (redis-py expõe o cliente como ``Redis``)."""

    def __init__(self, client):
        self.client = client

    def Redis(self, **kwargs):
        return self.client


class _FakeCtx:
    """``ctx`` falso do Hermes: captura o que ``register`` registra."""

    def __init__(self, store_conn=None, settings=None, secrets=None):
        self.registrations = []
        self.store_conn = store_conn
        self.settings = settings or {"log_statement": True}
        self.secrets = secrets or {}
        self.events = []

    # -- o que o host oferece --------------------------------------------
    def plugin_db(self):
        return self.store_conn

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def get_secret(self, name):
        return self.secrets.get(name)

    def broadcast_plugin_event(self, plugin_id, event, payload):
        self.events.append((plugin_id, event, dict(payload)))
        return True

    # -- o que register chama --------------------------------------------
    def register_tool(self, **kwargs):
        self.registrations.append(kwargs)
        return kwargs["name"]

    def tool(self, name):
        for call in self.registrations:
            if call["name"] == name:
                return call
        raise AssertionError(f"tool nao registrada: {name}")


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
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def db(tmp_path):
    conn = service.open_store(str(tmp_path / "connector.db"))
    yield conn
    conn.close()


@pytest.fixture()
def stored(db):
    return service.upsert_connection(db, _row())


@pytest.fixture(autouse=True)
def _isolated():
    """Nenhum wiring vaza entre testes."""
    tools.reset_providers()
    yield
    tools.reset_providers()
    redis_driver.forget_driver_module()


def _resolver(values=None):
    return (values or {"DB_PROD_SQLSERVER_PASSWORD": "senha-super-secreta"}).get


def _lines(db):
    return service.list_query_log(db)


def _connections(db, **overrides):
    service.upsert_connection(db, _row())
    service.upsert_connection(
        db,
        _row(
            id="local-pg",
            label="Postgres local",
            engine="postgres",
            environment="local",
            host="localhost",
            port=5432,
            database="app",
            secret_ref="DB_LOCAL_PG_PASSWORD",
        ),
    )
    service.upsert_connection(
        db,
        _row(
            id="cache-redis",
            label="Cache",
            engine="redis",
            environment="homolog",
            host="redis.internal",
            port=6379,
            database="2",
            secret_ref="",
        ),
    )
    if overrides.get("disabled"):
        service.upsert_connection(db, _row(id="old-sql", enabled=0))
    return db


def _run_redis(db, row, client, command, args=(), **kwargs):
    """Cycle through the **real** redis adapter with a fake library."""
    registry = DriverRegistry(overrides={"redis": _FakeRedisLib(client)})
    try:
        return redis_driver, tools.db_redis_command(
            {"connection_id": row["id"], "command": command, "args": list(args)},
            store=db,
            registry=registry,
            **kwargs,
        )
    finally:
        redis_driver.forget_driver_module()


# --------------------------------------------------------------------------
# 3.1 wrapper à prova de exceção
# --------------------------------------------------------------------------


def test_handler_que_lanca_vira_json_db_error_sem_stack_trace():
    def boom(args, **kwargs):
        raise RuntimeError("falha catastrofica: postgres://u:segredo123@10.0.0.9/db")

    answer = tools.guarded_handler(boom)({"statement": "SELECT 1"})

    assert isinstance(answer, str)
    payload = json.loads(answer)
    assert payload["ok"] is False
    assert payload["code"] == "db_error"
    assert "Traceback" not in answer
    assert "segredo123" not in answer
    assert "postgres://" not in answer


def test_handler_nunca_levanta_mesmo_com_args_invalidos():
    guarded = tools.guarded_handler(tools.db_query)

    for args in (None, "nao-e-dict", 42, []):
        payload = json.loads(guarded(args))
        assert payload["ok"] is False
        assert payload["code"] in service.ERROR_CODES


def test_driver_que_lanca_vira_json_string_db_error_sem_dsn(db, stored):
    driver = _FakeAdapter(raises=RuntimeError("boom postgres://u:segredo123@10.0.0.9/db"))

    answer = tools.guarded_handler(tools.db_query)(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    payload = json.loads(answer)  # JSON string: nunca uma excecao
    assert payload["ok"] is False
    assert payload["code"] == "db_error"
    assert "Traceback" not in answer
    assert "segredo123" not in answer
    assert "postgres://" not in answer
    assert _lines(db)[0]["status"] == "error"


def test_check_fn_sem_conexao_sinaliza_configuracao_pendente():
    tools.reset_providers()
    assert tools.check_connections_configured() is False


def test_check_fn_com_conexao_habilitada_sinaliza_ok(db, stored):
    tools.set_store(db)
    assert tools.check_connections_configured() is True

    service.upsert_connection(db, _row(id="prod-sqlserver", enabled=0))
    assert tools.check_connections_configured() is False  # nenhuma habilitada


def test_register_pluga_store_config_e_registra_as_tres_tools(db, tmp_path):
    ctx = _FakeCtx(store_conn=service.open_store(str(tmp_path / "register.db")))

    registered = tools.register_tools(ctx)

    assert registered == ["db_list_connections", "db_query", "db_redis_command"]
    assert [call["name"] for call in ctx.registrations] == registered
    for call in ctx.registrations:
        assert call["toolset"] == tools.TOOLSET
        assert callable(call["handler"])
        assert callable(call["check_fn"])
        assert call["schema"]["name"] == call["name"]
        assert isinstance(call["schema"]["parameters"], dict)
        assert call["description"]

    # store plugado: o handler enxerga as conexões sem receber nada
    service.upsert_connection(ctx.store_conn, _row())
    answer = json.loads(ctx.tool("db_list_connections")["handler"]({}))
    assert answer["ok"] is True
    assert answer["connections"][0]["id"] == "prod-sqlserver"

    # config provider do host plugado no service (toggle log_statement)
    assert service.get_config("log_statement", False) is True


def test_provides_tools_do_manifesto_bate_com_as_tools_registradas(tmp_path):
    """Task 3.5: zero divergence between manifest and ``ctx.register_tool``."""
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    ctx = _FakeCtx(store_conn=service.open_store(str(tmp_path / "manifest.db")))

    registered = tools.register_tools(ctx)

    # mesma fonte dos dois lados: o que o ctx falso capturou x o que o YAML diz
    assert sorted(call["name"] for call in ctx.registrations) == sorted(registered)
    assert sorted(manifest["provides_tools"]) == sorted(registered)
    assert sorted(manifest["provides_tools"]) == sorted(tools.TOOL_NAMES)
    assert set(manifest["provides_tools"]) == {
        "db_list_connections",
        "db_query",
        "db_redis_command",
    }


def test_register_sobrevive_a_ctx_sem_nada():
    class _Bare:
        def register_tool(self, **kwargs):
            raise RuntimeError("registry indisponivel")

    assert tools.register_tools(_Bare()) == []


# --------------------------------------------------------------------------
# 3.2 db_list_connections
# --------------------------------------------------------------------------


def test_lista_conexoes_habilitadas_sem_segredo(db):
    _connections(db, disabled=True)
    registry = DriverRegistry(overrides={"sqlserver": object(), "postgres": object(), "redis": object()})

    answer = tools.db_list_connections({}, store=db, registry=registry)

    assert answer["ok"] is True
    assert answer["count"] == 3
    assert [item["id"] for item in answer["connections"]] == [
        "cache-redis",
        "local-pg",
        "prod-sqlserver",
    ]
    first = answer["connections"][2]
    assert set(first) == {
        "id",
        "label",
        "engine",
        "environment",
        "host",
        "port",
        "database",
        "read_only",
    }
    assert first["read_only"] is True
    rendered = json.dumps(answer, ensure_ascii=False)
    assert "secret_ref" not in rendered
    assert "senha-super-secreta" not in rendered
    assert "DB_PROD_SQLSERVER_PASSWORD" not in rendered
    assert "username" not in rendered


def test_filtro_por_environment(db):
    _connections(db)
    registry = DriverRegistry(overrides={"sqlserver": object(), "postgres": object(), "redis": object()})

    answer = tools.db_list_connections({"environment": "prod"}, store=db, registry=registry)

    assert answer["ok"] is True
    assert [item["id"] for item in answer["connections"]] == ["prod-sqlserver"]

    empty = tools.db_list_connections({"environment": "local"}, store=db, registry=registry)
    assert [item["id"] for item in empty["connections"]] == ["local-pg"]


def test_engine_sem_driver_e_marcada(db):
    _connections(db)
    # sqlserver sem driver no host; as outras seguem normais
    registry = DriverRegistry(overrides={"sqlserver": None, "postgres": object(), "redis": object()})

    answer = tools.db_list_connections({}, store=db, registry=registry)
    by_id = {item["id"]: item for item in answer["connections"]}

    assert by_id["prod-sqlserver"]["driver_missing"] is True
    assert "driver_missing" not in by_id["local-pg"]
    assert "driver_missing" not in by_id["cache-redis"]


def test_lista_sem_store_configurado():
    tools.reset_providers()
    answer = tools.db_list_connections({})
    assert answer["ok"] is False
    assert answer["code"] == "db_error"


# --------------------------------------------------------------------------
# 3.3 db_query
# --------------------------------------------------------------------------


def test_select_ok_devolve_linhas(db, stored):
    driver = _FakeAdapter(
        {"rows": [[1, "Ana"], [2, "Bob"]], "row_count": 2, "truncated": False, "duration_ms": 7}
    )

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT id, nome FROM clientes"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert answer["ok"] is True
    assert answer["rows"] == [[1, "Ana"], [2, "Bob"]]
    assert answer["row_count"] == 2
    assert answer["truncated"] is False
    assert answer["duration_ms"] == 7
    assert answer["connection"] == {
        "id": "prod-sqlserver",
        "engine": "sqlserver",
        "environment": "prod",
    }
    assert ("execute",) == (driver.calls[-1][0],)


def test_cte_de_leitura_e_aceita(db, stored):
    driver = _FakeAdapter()

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "WITH ativos AS (SELECT 1) SELECT * FROM ativos"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert answer["ok"] is True
    assert driver.calls[-1][2].startswith("WITH")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE clientes SET nome = 'x'",
        "DELETE FROM clientes",
        "INSERT INTO clientes VALUES (1)",
        "MERGE INTO clientes USING origem ON 1 = 1",
        "CREATE TABLE t (id int)",
        "ALTER TABLE t ADD col int",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "SET TRANSACTION READ ONLY",
        "EXEC sp_quem",
    ],
)
def test_escrita_ddl_e_sessao_sao_recusados_sem_contato_com_driver(db, stored, statement):
    driver = _FakeAdapter()
    emitter = _Recorder()

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": statement},
        store=db,
        resolver=_resolver(),
        driver=driver,
        emitter=emitter,
    )

    assert answer["ok"] is False
    assert answer["code"] == "read_only"
    assert "read_only" in service.ERROR_CODES
    # recusa aconteceu antes do driver: nenhum connect/execute
    assert driver.calls == []
    # e ficou registrada no ciclo: running -> error + query.failed
    lines = _lines(db)
    assert len(lines) == 1
    assert lines[0]["status"] == "error"
    assert "apenas leitura" in (lines[0]["error"] or "")
    assert emitter.names() == ["query.started", "query.failed"]


def test_lote_com_ponto_e_virgula_e_recusado(db, stored):
    driver = _FakeAdapter()

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1; DELETE FROM clientes"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert answer["code"] == "read_only"
    assert driver.calls == []
    assert len(_lines(db)) == 1


def test_comentarios_sao_recusados(db, stored):
    driver = _FakeAdapter()

    line = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1 -- comenta"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )
    block = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT /* segredo */ 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert line["code"] == "read_only"
    assert block["code"] == "read_only"
    assert driver.calls == []


def test_ponto_e_virgula_unico_no_fim_e_aceito(db, stored):
    driver = _FakeAdapter()

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1;"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert answer["ok"] is True


def test_conexao_inexistente_devolve_not_found_sem_linha_running(db, stored):
    driver = _FakeAdapter()
    emitter = _Recorder()

    answer = tools.db_query(
        {"connection_id": "nao-existe", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
        emitter=emitter,
    )

    assert answer["ok"] is False
    assert answer["code"] == "not_found"
    assert driver.calls == []
    assert _lines(db) == []
    assert emitter.events == []


def test_conexao_desabilitada_tambem_e_not_found(db, stored):
    service.upsert_connection(db, _row(id="prod-sqlserver", enabled=0))

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"}, store=db
    )

    assert answer["code"] == "not_found"


def test_sem_connection_id_e_not_found(db, stored):
    answer = tools.db_query({"statement": "SELECT 1"}, store=db)
    assert answer["code"] == "not_found"
    assert _lines(db) == []


def test_segredo_ausente_devolve_secret_missing_sem_linha_running(db, stored):
    emitter = _Recorder()

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver({"DB_PROD_SQLSERVER_PASSWORD": None}),
        driver=_FakeAdapter(),
        emitter=emitter,
    )

    assert answer["ok"] is False
    assert answer["code"] == "secret_missing"
    assert _lines(db) == []
    assert emitter.events == []


def test_driver_missing_traz_install_e_nota_de_reinicio(db, stored):
    registry = DriverRegistry(overrides={"sqlserver": None})

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        registry=registry,
    )

    assert answer["ok"] is False
    assert answer["code"] == "driver_missing"
    assert "pip install" in answer["install"]
    assert "pymssql" in answer["install"]
    assert answer["note"] == tools.NOTE_RESTART
    assert _lines(db) == []  # pre-flight: nada é logado


def test_timeout_devolve_code_timeout_e_linha_error(db, stored):
    driver = _FakeAdapter(raises=TimeoutError("statement timeout exceeded"))

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert answer["ok"] is False
    assert answer["code"] == "timeout"
    assert _lines(db)[0]["status"] == "error"


def test_erro_de_driver_vira_db_error(db, stored):
    driver = _FakeAdapter(raises=RuntimeError("tabela inexistente"))

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )

    assert answer["code"] == "db_error"
    assert answer["ok"] is False


def test_max_rows_teto_de_1000_e_padrao_200(db, stored):
    driver = _FakeAdapter()
    tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1", "max_rows": 5000},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )
    assert driver.calls[-1][3] == (1000,)  # pediu 5000 -> teto

    tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )
    assert driver.calls[-1][3] == (200,)

    tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1", "max_rows": 10},
        store=db,
        resolver=_resolver(),
        driver=driver,
    )
    assert driver.calls[-1][3] == (10,)


def test_db_query_grava_log_ok_e_emite_started_finished(db, stored):
    emitter = _Recorder()
    driver = _FakeAdapter({"rows": [[1]], "row_count": 1, "truncated": False, "duration_ms": 5})

    answer = tools.db_query(
        {"connection_id": "prod-sqlserver", "statement": "SELECT 1"},
        store=db,
        resolver=_resolver(),
        driver=driver,
        emitter=emitter,
        session_id="sessao-tools",
    )

    assert answer["ok"] is True
    lines = _lines(db)
    assert len(lines) == 1
    assert lines[0]["status"] == "ok"
    assert lines[0]["source"] == "agent"
    assert lines[0]["session_id"] == "sessao-tools"
    assert "senha-super-secreta" not in str(lines[0])
    assert emitter.names() == ["query.started", "query.finished"]


# --------------------------------------------------------------------------
# 3.4 db_redis_command
# --------------------------------------------------------------------------


@pytest.fixture()
def redis_row(db):
    return service.upsert_connection(
        db,
        _row(
            id="cache-redis",
            label="Cache",
            engine="redis",
            environment="homolog",
            port=6379,
            database="2",
            secret_ref="",
        ),
    )


@pytest.mark.parametrize("command", ["GET", "HGETALL", "SCAN", "ZRANGE", "TTL", "INFO"])
def test_comandos_da_allowlist_executam(db, redis_row, command):
    client = _FakeRedisClient({"campo": "valor"})

    _adapter, answer = _run_redis(db, redis_row, client, command, ("chave",))

    assert answer["ok"] is True
    assert answer["value"] == {"campo": "valor"}
    assert answer["connection"]["engine"] == "redis"
    assert client.calls == [(command, ("chave",))]
    assert _lines(db)[0]["status"] == "ok"


@pytest.mark.parametrize(
    "command",
    ["FLUSHALL", "FLUSHDB", "EVAL", "SCRIPT", "KEYS", "CONFIG", "AUTH", "SET", "DEL", "MEMORY", "OBJECT", "CLIENT", "DEBUG", "SHUTDOWN", "CLUSTER", "REPLICAOF"],
)
def test_comando_fora_da_allowlist_e_recusado_sem_tocar_servidor(db, redis_row, command):
    client = _FakeRedisClient()
    emitter = _Recorder()

    answer = tools.db_redis_command(
        {"connection_id": "cache-redis", "command": command, "args": ["*"]},
        store=db,
        registry=DriverRegistry(overrides={"redis": _FakeRedisLib(client)}),
        emitter=emitter,
    )

    assert answer["ok"] is False
    assert answer["code"] == "read_only"
    assert client.calls == []  # nenhum contato com o servidor
    assert _lines(db) == []  # nem linha "running"
    assert emitter.events == []


def test_comando_minusculo_e_normalizado(db, redis_row):
    client = _FakeRedisClient("valor")

    _adapter, answer = _run_redis(db, redis_row, client, "get", ("minha:chave",))

    assert answer["ok"] is True
    assert client.calls == [("GET", ("minha:chave",))]


def test_comando_vazio_e_recusado(db, redis_row):
    answer = tools.db_redis_command({"connection_id": "cache-redis", "command": ""}, store=db)
    assert answer["code"] == "read_only"


def test_redis_valor_truncado_em_64_kib(db, redis_row):
    big = "x" * (100 * 1024)
    client = _FakeRedisClient(big)

    _adapter, answer = _run_redis(db, redis_row, client, "GET", ("grande",))

    assert answer["ok"] is True
    assert answer["truncated"] is True
    assert len(answer["value"].encode("utf-8")) == 64 * 1024


def test_redis_conexao_inexistente_e_not_found(db, redis_row):
    answer = tools.db_redis_command(
        {"connection_id": "outra", "command": "GET"}, store=db
    )
    assert answer["code"] == "not_found"
    assert _lines(db) == []


def test_redis_driver_missing_traz_install_e_nota(db, redis_row):
    answer = tools.db_redis_command(
        {"connection_id": "cache-redis", "command": "GET"},
        store=db,
        registry=DriverRegistry(overrides={"redis": None}),
    )

    assert answer["code"] == "driver_missing"
    assert "redis" in answer["install"]
    assert answer["note"] == tools.NOTE_RESTART


# --------------------------------------------------------------------------
# 3.1 as três tools registradas: handler sempre JSON string
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        ("db_list_connections", {}),
        ("db_query", {"connection_id": "prod-sqlserver", "statement": "SELECT 1"}),
        ("db_redis_command", {"connection_id": "cache-redis", "command": "GET"}),
    ],
)
def test_handler_registrado_devolve_json_string(db, stored, redis_row, name, args):
    registry = DriverRegistry(
        overrides={"sqlserver": object(), "postgres": object(), "redis": object()}
    )
    handlers = {tool_name: handler for tool_name, _schema, handler in tools.TOOL_DEFINITIONS}

    answer = tools.guarded_handler(handlers[name])(args, store=db, registry=registry)

    assert isinstance(answer, str)
    payload = json.loads(answer)
    assert "ok" in payload
