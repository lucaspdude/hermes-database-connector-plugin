"""Task 1.2 — ``drivers/postgres.py``: READ ONLY, timeouts, limited fetch.

Every test injects a **fake** psycopg (the real driver is not installed here):
no server is contacted. The validation against a real Postgres is still pending
(local/Docker + host ``hostgator``), which is also what covers the behaviour of
a genuine ``SET TRANSACTION READ ONLY`` refusing a disguised write.
"""

import pytest

import service
from drivers import DriverRegistry
from drivers import postgres


def _row(**overrides):
    payload = {
        "id": "local-pg",
        "engine": "postgres",
        "environment": "local",
        "host": "pg.local",
        "port": 5432,
        "database": "hub",
        "username": "hermes_ro",
        "secret_ref": "DB_LOCAL_PG_PASSWORD",
        "options": {"sslmode": "require", "connect_timeout_s": 4, "statement_timeout_s": 7},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, client):
        self.client = client
        self.rows = client.rows
        self.rowcount = -1
        self.closed = False

    def execute(self, statement):
        self.client.executed.append(statement)
        if self.client.raises_on and self.client.raises_on in statement:
            raise RuntimeError(f"boom: {statement}")

    def fetchmany(self, size):
        got = self.rows[:size]
        self.rowcount = len(self.rows)
        return got

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        self.closed = True
        self.client.cursors_closed += 1


class _FakeClient:
    def __init__(self, rows=(), version="17.2", raises_on=None):
        self.rows = [list(row) for row in rows]
        self.server_version = version
        self.raises_on = raises_on
        self.executed: list[str] = []
        self.rollbacks = 0
        self.cursors_closed = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakePsycopg:
    def __init__(self, client):
        self.client = client
        self.kwargs: dict = {}

    def connect(self, **kwargs):
        self.kwargs = kwargs
        return self.client

    # psycopg expõe a versão via ``client.info``; aqui ``None`` basta
    @property
    def __name__(self):  # pragma: no cover - cosmetic
        return "psycopg"


@pytest.fixture(autouse=True)
def _isolated_module():
    postgres.forget_driver_module()
    yield
    postgres.forget_driver_module()


def _install(client) -> _FakePsycopg:
    fake = _FakePsycopg(client)
    postgres.set_driver_module(fake)
    return fake


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------


def test_connect_passes_sslmode_and_timeouts():
    fake = _install(_FakeClient())

    client = postgres.connect(_row(), "s3nh@")

    assert client is fake.client
    assert fake.kwargs["host"] == "pg.local"
    assert fake.kwargs["port"] == 5432
    assert fake.kwargs["dbname"] == "hub"
    assert fake.kwargs["user"] == "hermes_ro"
    assert fake.kwargs["password"] == "s3nh@"
    assert fake.kwargs["sslmode"] == "require"
    assert fake.kwargs["connect_timeout"] == 4


def test_connect_without_options_uses_documented_defaults():
    fake = _install(_FakeClient())
    postgres.connect({"engine": "postgres", "host": "pg.local"}, None)
    assert fake.kwargs["connect_timeout"] == postgres.DEFAULT_CONNECT_TIMEOUT_S
    assert "sslmode" not in fake.kwargs
    assert "password" not in fake.kwargs


# --------------------------------------------------------------------------
# execute: transação READ ONLY + limites + cleanup
# --------------------------------------------------------------------------


def test_execute_declares_read_only_before_the_statement():
    client = _FakeClient(rows=[("a", 1), ("b", 2)])
    _install(client)
    opened = postgres.connect(_row(), "pw")  # leva o statement_timeout_s=7

    result = postgres.execute(opened, "SELECT * FROM t", 200)

    # ordem: timeout, READ ONLY, statement — dentro da mesma transação
    assert client.executed[0].startswith("SET statement_timeout = 7000")
    assert client.executed[1] == postgres.READ_ONLY_STATEMENT
    assert client.executed[-1] == "SELECT * FROM t"
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert result["server_version"] == "17.2"


def test_execute_limits_rows_and_flags_truncation():
    client = _FakeClient(rows=[(i,) for i in range(5)])
    _install(client)

    result = postgres.execute(client, "SELECT * FROM big", 2)

    assert result["rows"] == [[0], [1]]
    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_execute_rolls_back_and_closes_even_on_error():
    client = _FakeClient(raises_on="SELECT")
    _install(client)

    with pytest.raises(RuntimeError):
        postgres.execute(client, "SELECT * FROM t", 200)


    assert client.rollbacks == 1, "a transação não pode sobreviver ao erro"
    assert client.cursors_closed == 1


def test_probe_and_close():
    client = _FakeClient(rows=[(1,)])
    _install(client)

    assert postgres.probe(client) == {"server_version": "17.2"}
    assert client.executed == [postgres.PROBE_STATEMENT]
    postgres.close(client)
    assert client.closed is True


# --------------------------------------------------------------------------
# interface uniforme (mesmo shape do sqlserver) + integração
# --------------------------------------------------------------------------


def test_result_shape_is_the_uniform_one():
    """Mesmo shape das duas engines: o ciclo de consulta não sabe a engine."""
    from drivers import sqlserver

    # o mesmo fake serve às duas engines: é exatamente o contrato uniforme
    pg_client = _FakeClient(rows=[(1,)])
    _install(pg_client)
    ms_client = _FakeClient(rows=[(1,)], version="16.0.1000")

    pg_result = postgres.execute(pg_client, "SELECT 1", 10)
    ms_result = sqlserver.execute(ms_client, "SELECT 1", 10)

    assert set(pg_result) == set(ms_result)
    for key in ("rows", "row_count", "truncated", "server_version"):
        assert key in pg_result and key in ms_result
    assert pg_result["rows"] == ms_result["rows"] == [[1]]


def test_probe_agrees_with_test_connection_server_version():
    """Integração: a versão que ``test_connection`` reporta é a do mesmo probe."""
    client = _FakeClient(rows=[(1,)], version="17.2")
    fake = _install(client)
    registry = DriverRegistry({"postgres": fake})
    connection = _row()

    result = service.test_connection(connection, registry, lambda _name: "pw")

    assert result["ok"] is True, result
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert result["server_version"] == "17.2"
    assert postgres.probe(fake.client)["server_version"] == result["server_version"]
