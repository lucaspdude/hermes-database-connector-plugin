"""Task 3.6 — a transação ``READ ONLY`` barra o que o filtro não vê.

O filtro da tool (``tools.refuse_statement``) aceita ``WITH ...`` como CTE de
leitura: uma escrita disfarçada de CTE (``WITH x AS (...) DELETE FROM ...``)
**passa** por ele. É a transação ``SET TRANSACTION READ ONLY`` — no PostgreSQL
(task 1.2) e no SQL Server (task 1.5) — que faz o próprio banco recusar a
operação, e é isso que estes testes cobrem, nas duas engines.

Os fakes de psycopg e pymssql simulam o servidor: no PostgreSQL, depois de
``SET TRANSACTION READ ONLY`` qualquer escrita levanta o erro que o banco real
levantaria e **nada é mutado** (``mutations`` continua vazio e as linhas seguem
intactas). Sem a declaração, a mesma statement mutaria a "tabela" do fake — é
esse contraste que prova o efeito da transação.

No SQL Server **não existe** ``SET TRANSACTION READ ONLY`` (task 1.5): enviá-la
faria o servidor devolver erro de sintaxe em toda consulta, e por isso a
declaração nunca é emitida (teste negativo). Aqui o fake T-SQL falha se a
receber e recusa a escrita porque simula a **credencial somente-leitura** —
pré-requisito de uso e garantia primária desta engine (README/``Política
read-only``).

Não há servidor nem driver real aqui: a validação contra instâncias reais
segue pendente (host ``hostgator``/bateria).
"""

import re

import pytest

import service
import db_tools as tools
from drivers import postgres
from drivers import sqlserver

#: Escrita disfarçada de CTE — o filtro da tool **não** a recusa.
DISGUISED_STATEMENT = (
    "WITH alvos AS (SELECT id FROM pedidos WHERE status = 'cancelado') "
    "DELETE FROM pedidos WHERE id IN (SELECT id FROM alvos)"
)

READ_ONLY_VIOLATION = "cannot execute DELETE in a read-only transaction"

#: O que o "servidor" considera escrita: o verbo em qualquer posição da
#: statement — é assim que o ``DELETE`` do ``WITH ... DELETE`` aparece (o lead
#: da statement é ``WITH``, e por isso o filtro da tool não o vê).
_WRITE_RE = re.compile(r"\b(DELETE|UPDATE|INSERT|MERGE|CREATE|ALTER|DROP|TRUNCATE|INTO)\b")


def _is_write(statement: str) -> bool:
    """True quando o banco executaria escrita (inclui ``SELECT ... INTO``)."""
    return bool(_WRITE_RE.search(str(statement).upper()))


# --------------------------------------------------------------------------
# fakes — um "servidor" que obedece à transação READ ONLY
# --------------------------------------------------------------------------


class _ServerCursor:
    def __init__(self, client):
        self.client = client
        self.fetched: list[list] = []
        self.rowcount = -1
        self.closed = False

    def execute(self, statement):
        client = self.client
        client.executed.append(statement)
        text = str(statement).strip()
        upper = text.upper()
        if upper.startswith("SET TRANSACTION READ ONLY"):
            if client.tsql:
                # T-SQL nao tem a declaracao: um SQL Server real devolveria
                # erro de sintaxe — 1.5 prova que ela nunca é enviada.
                raise RuntimeError(sqlserver.READ_ONLY_TSQL_ERROR)
            client.read_only = True
            return
        if upper.startswith("SET STATEMENT_TIMEOUT"):
            return
        if _is_write(text):
            if client.read_only or client.read_only_login:
                # É o banco recusando, não o plugin: sem READ ONLY mutaria.
                raise RuntimeError(READ_ONLY_VIOLATION)
            client.mutations.append(text)
            client.rows = []  # efeito real de um DELETE
            return
        self.fetched = client.rows
        self.rowcount = len(client.rows)

    def fetchmany(self, size):
        return self.fetched[:size]

    def fetchall(self):
        return self.fetched

    def fetchone(self):
        return self.fetched[0] if self.fetched else None

    def close(self):
        self.closed = True
        self.client.cursors_closed += 1


class _ServerClient:
    """Cliente falso: guarda o que foi executado e o que foi mutado."""

    def __init__(
        self,
        rows=(("10", "aberto"), ("20", "pago")),
        version="17.2",
        *,
        tsql: bool = False,
        read_only_login: bool = False,
    ):
        self.rows = [list(row) for row in rows]
        self.server_version = version
        self.tsql = tsql  # servidor T-SQL: recusa a declaracao READ ONLY
        self.read_only_login = read_only_login  # credencial somente-leitura
        self.executed: list[str] = []
        self.mutations: list[str] = []
        self.read_only = False
        self.rollbacks = 0
        self.cursors_closed = 0
        self.timeout = None
        self.closed = False

    def cursor(self):
        return _ServerCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def first_execution_of(self, fragment: str) -> int:
        for index, executed in enumerate(self.executed):
            if fragment in executed:
                return index
        raise AssertionError(f"statement nao executada: {fragment}")


class _FakeLib:
    """Stand-in de psycopg/pymssql: entrega o cliente e conta conexões."""

    def __init__(self, client):
        self.client = client
        self.connect_calls = 0

    def connect(self, *args, **kwargs):
        self.connect_calls += 1
        return self.client


@pytest.fixture()
def pg_server():
    client = _ServerClient(version="17.2")
    postgres.set_driver_module(_FakeLib(client))
    yield client
    postgres.forget_driver_module()


@pytest.fixture()
def mssql_server():
    client = _ServerClient(
        rows=(("10", "aberto"),),
        version="16.0.1000",
        tsql=True,
        read_only_login=True,  # garantia primaria: credencial somente-leitura
    )
    sqlserver.set_driver_module(_FakeLib(client), optional=None)
    sqlserver._pyodbc_preferred = False
    yield client
    sqlserver.forget_driver_module()
    sqlserver._pyodbc_preferred = False


def _row(engine: str, **overrides):
    payload = {
        "id": f"ro-{engine}",
        "label": f"Leitura {engine}",
        "engine": engine,
        "environment": "prod",
        "host": f"{engine}.prod.local",
        "port": 5432 if engine == "postgres" else 1433,
        "database": "hub",
        "username": "hermes_ro",
        "secret_ref": f"DB_{engine.upper()}_PASSWORD",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def db(tmp_path):
    conn = service.open_store(str(tmp_path / "read_only.db"))
    yield conn
    conn.close()


def _resolver(values=None):
    return (values or {"DB_POSTGRES_PASSWORD": "s3nh@", "DB_SQLSERVER_PASSWORD": "s3nh@"}).get


# --------------------------------------------------------------------------
# o filtro da tool não pega o caso (premissa do cenário)
# --------------------------------------------------------------------------


def test_filtro_da_tool_nao_recusa_a_escrita_disfarcada():
    assert tools.refuse_statement(DISGUISED_STATEMENT) is None
    assert tools.leading_keyword(DISGUISED_STATEMENT) == "with"


def test_filtro_da_tool_recusa_set_e_exec():
    for statement in ("SET TRANSACTION READ ONLY", "EXEC sp_who", "EXECUTE dbo.limpa"):
        assert tools.refuse_statement(statement), statement


# --------------------------------------------------------------------------
# PostgreSQL (driver)
# --------------------------------------------------------------------------


def test_postgres_read_only_vem_antes_e_o_banco_recusa_sem_efeito(pg_server):
    expected_rows = [list(row) for row in pg_server.rows]

    with pytest.raises(RuntimeError) as caught:
        postgres.execute(pg_server, DISGUISED_STATEMENT, 200)

    # o READ ONLY foi declarado ANTES da statement que o banco recusou
    read_only_at = pg_server.first_execution_of(postgres.READ_ONLY_STATEMENT)
    statement_at = pg_server.first_execution_of("DELETE FROM pedidos")
    assert read_only_at < statement_at
    assert READ_ONLY_VIOLATION in str(caught.value)
    # sem efeito: nada mutado, linhas intactas e transação revertida
    assert pg_server.mutations == []
    assert pg_server.rows == expected_rows
    assert pg_server.rollbacks == 1
    assert pg_server.cursors_closed == 1


def test_postgres_sem_read_only_a_escrita_teria_efeito():
    """Contraste: é a transação que barra — o servidor aceita quando não há."""
    client = _ServerClient()
    cursor = _ServerCursor(client)

    cursor.execute(DISGUISED_STATEMENT)

    assert client.read_only is False
    assert len(client.mutations) == 1
    assert client.rows == []


# --------------------------------------------------------------------------
# SQL Server (driver)
# --------------------------------------------------------------------------


def test_fake_sqlserver_falha_se_receber_a_declaracao(mssql_server):
    """Negativo de 1.5: o servidor T-SQL recusa ``SET TRANSACTION READ ONLY``."""
    with pytest.raises(RuntimeError) as caught:
        mssql_server.cursor().execute(sqlserver.READ_ONLY_STATEMENT)

    assert sqlserver.READ_ONLY_TSQL_ERROR in str(caught.value)


def test_sqlserver_nao_envia_a_declaracao_e_o_banco_recusa_sem_efeito(mssql_server):
    """1.5/3.6: nenhuma declaração é enviada; quem recusa é a credencial."""
    expected_rows = [list(row) for row in mssql_server.rows]

    with pytest.raises(RuntimeError) as caught:
        sqlserver.execute(mssql_server, DISGUISED_STATEMENT, 200)

    # nenhuma declaração READ ONLY chegou ao servidor (T-SQL não a tem)
    assert sqlserver.READ_ONLY_STATEMENT not in mssql_server.executed
    assert mssql_server.first_execution_of("DELETE FROM pedidos") == 0
    assert READ_ONLY_VIOLATION in str(caught.value)
    assert mssql_server.mutations == []
    assert mssql_server.rows == expected_rows
    assert mssql_server.rollbacks == 1
    assert mssql_server.cursors_closed == 1


# --------------------------------------------------------------------------
# pela tool: o erro do banco volta como db_error, sem efeito
# --------------------------------------------------------------------------


def test_tool_postgres_devolve_db_error_sem_efeito(db, pg_server):
    expected_rows = [list(row) for row in pg_server.rows]
    service.upsert_connection(db, _row("postgres"))

    answer = tools.db_query(
        {"connection_id": "ro-postgres", "statement": DISGUISED_STATEMENT},
        store=db,
        resolver=_resolver(),
        driver=postgres,
    )

    assert answer["ok"] is False
    assert answer["code"] == service.CODE_DB_ERROR
    assert READ_ONLY_VIOLATION in answer["error"]
    assert pg_server.first_execution_of(postgres.READ_ONLY_STATEMENT) < pg_server.first_execution_of(
        "DELETE FROM pedidos"
    )
    # sem efeito no "banco" e com a transação revertida
    assert pg_server.mutations == []
    assert pg_server.rows == expected_rows
    assert pg_server.rollbacks == 1

    log = service.list_query_log(db)
    assert len(log) == 1
    assert log[0]["status"] == "error"


def test_tool_sqlserver_devolve_db_error_sem_efeito(db, mssql_server):
    expected_rows = [list(row) for row in mssql_server.rows]
    service.upsert_connection(db, _row("sqlserver"))

    answer = tools.db_query(
        {"connection_id": "ro-sqlserver", "statement": DISGUISED_STATEMENT},
        store=db,
        resolver=_resolver(),
        driver=sqlserver,
    )

    assert answer["ok"] is False
    assert answer["code"] == service.CODE_DB_ERROR
    assert READ_ONLY_VIOLATION in answer["error"]
    # nada de declaração READ ONLY: T-SQL não a tem (quem recusa é a credencial)
    assert sqlserver.READ_ONLY_STATEMENT not in mssql_server.executed
    assert mssql_server.mutations == []
    assert mssql_server.rows == expected_rows
    assert mssql_server.rollbacks == 1

    log = service.list_query_log(db)
    assert len(log) == 1
    assert log[0]["status"] == "error"


# --------------------------------------------------------------------------
# pela tool: SET e EXEC nem chegam ao driver
# --------------------------------------------------------------------------


@pytest.mark.parametrize("statement", ["SET TRANSACTION READ ONLY", "EXEC dbo.limpa_tudo"])
@pytest.mark.parametrize("engine", ["postgres", "sqlserver"])
def test_tool_recusa_set_e_exec_sem_falar_com_o_driver(db, engine, statement, request):
    client = request.getfixturevalue("pg_server" if engine == "postgres" else "mssql_server")
    adapter = postgres if engine == "postgres" else sqlserver
    service.upsert_connection(db, _row(engine))

    answer = tools.db_query(
        {"connection_id": f"ro-{engine}", "statement": statement},
        store=db,
        resolver=_resolver(),
        driver=adapter,
    )

    assert answer["ok"] is False
    assert answer["code"] == service.CODE_READ_ONLY
    assert answer.get("rows") is None
    # nada chegou ao driver: nenhuma statement executada
    assert client.executed == []

    log = service.list_query_log(db)
    assert len(log) == 1
    assert log[0]["status"] == "error"
