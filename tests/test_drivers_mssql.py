"""Tasks 1.3 e 1.5 — ``drivers/sqlserver.py``: pymssql padrão, pyodbc opcional,
transação READ ONLY envolvendo a execução.

Fakes only: ``pymssql``/``pyodbc`` are not installed here, so no server is
contacted. The real-instance validation (SQL Server on host ``hostgator``,
and the pyodbc path, which also needs MS ODBC Driver 18 on the host) is still
pending.
"""

import pytest

import service
from drivers import DriverRegistry
from drivers import sqlserver


def _row(**overrides):
    payload = {
        "id": "prod-sqlserver",
        "engine": "sqlserver",
        "environment": "prod",
        "host": "sql.prod.local",
        "port": 1433,
        "database": "HubFinanceira",
        "username": "hermes_ro",
        "secret_ref": "DB_PROD_SQLSERVER_PASSWORD",
        "options": {
            "connect_timeout_s": 5,
            "statement_timeout_s": 8,
            "tds_version": "7.4",
            "trust_server_certificate": True,
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, client):
        self.client = client
        self.rowcount = -1
        self.closed = False

    def execute(self, statement):
        self.client.executed.append(statement)
        if self.client.raises_on and self.client.raises_on in statement:
            raise RuntimeError(f"boom: {statement}")

    def fetchmany(self, size):
        self.rowcount = len(self.client.rows)
        return self.client.rows[:size]

    def fetchall(self):
        return self.client.rows

    def fetchone(self):
        return self.client.rows[0] if self.client.rows else None

    def close(self):
        self.closed = True
        self.client.cursors_closed += 1


class _FakeClient:
    def __init__(self, rows=(), version="16.0.1000", raises_on=None):
        self.rows = [list(row) for row in rows]
        self.server_version = version
        self.raises_on = raises_on
        self.executed: list[str] = []
        self.rollbacks = 0
        self.cursors_closed = 0
        self.closed = False
        self.timeout = None

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakePymssql:
    """Stand-in for pymssql: records kwargs and rejects unknown ones."""

    def __init__(self, client):
        self.client = client
        self.kwargs: dict = {}
        self.connect_calls = 0

    def connect(self, **kwargs):
        self.connect_calls += 1
        for key in kwargs:
            if key not in (
                "server",
                "user",
                "password",
                "database",
                "login_timeout",
                "timeout",
                "tds_version",
                "charset",
                "appname",
            ):
                raise TypeError(f"unexpected keyword argument '{key}'")
        self.kwargs = kwargs
        return self.client


class _FakePyodbc:
    """Stand-in for pyodbc: connection-string based."""

    def __init__(self, client):
        self.client = client
        self.connection_strings: list[str] = []
        self.kwargs_calls: list[dict] = []

    def connect(self, connection_string=None, **kwargs):
        if connection_string is None:
            raise TypeError("missing connection string")
        if kwargs:
            self.kwargs_calls.append(kwargs)
        self.connection_strings.append(connection_string)
        return self.client


@pytest.fixture(autouse=True)
def _isolated_module():
    sqlserver.forget_driver_module()
    sqlserver._pyodbc_preferred = False
    yield
    sqlserver.forget_driver_module()
    sqlserver._pyodbc_preferred = False


# --------------------------------------------------------------------------
# 1.3 caminho padrão pymssql
# --------------------------------------------------------------------------


def test_connect_pymssql_sends_login_and_statement_timeouts():
    client = _FakeClient()
    pymssql = _FakePymssql(client)
    sqlserver.set_driver_module(pymssql, optional=None)  # pyodbc ausente

    opened = sqlserver.connect(_row(), "s3nh@")

    assert opened is client
    assert pymssql.kwargs["server"] == "sql.prod.local:1433"
    assert pymssql.kwargs["user"] == "hermes_ro"
    assert pymssql.kwargs["password"] == "s3nh@"
    assert pymssql.kwargs["database"] == "HubFinanceira"
    assert pymssql.kwargs["login_timeout"] == 5
    assert pymssql.kwargs["timeout"] == 8
    assert pymssql.kwargs["tds_version"] == "7.4"
    # pymssql não aceita trust_server_certificate: cai fora sem quebrar
    assert "trust_server_certificate" not in pymssql.kwargs
    assert pymssql.connect_calls == 2  # tentativa com extra + retry sem ela


def test_connect_without_options_uses_documented_defaults():
    pymssql = _FakePymssql(_FakeClient())
    sqlserver.set_driver_module(pymssql, optional=None)

    sqlserver.connect({"engine": "sqlserver", "host": "sql.local"}, None)

    assert pymssql.kwargs["server"] == "sql.local"
    assert pymssql.kwargs["login_timeout"] == sqlserver.DEFAULT_CONNECT_TIMEOUT_S
    assert pymssql.kwargs["timeout"] == sqlserver.DEFAULT_STATEMENT_TIMEOUT_S
    assert "password" not in pymssql.kwargs


def test_pyodbc_absent_falls_back_to_pymssql():
    """Sem pyodbc no host, o caminho padrão segue sendo o pymssql."""
    client = _FakeClient()
    pymssql = _FakePymssql(client)
    sqlserver.set_driver_module(pymssql, optional=None)

    assert sqlserver.use_pyodbc(_row()) is False
    assert sqlserver.connect(_row(), "pw") is client
    assert pymssql.connect_calls >= 1


# --------------------------------------------------------------------------
# 1.3 caminho opcional pyodbc (detectado por find_spec)
# --------------------------------------------------------------------------


def test_pyodbc_present_is_used_with_native_tls_and_timeouts():
    client = _FakeClient()
    pyodbc = _FakePyodbc(client)
    sqlserver.set_driver_module(_FakePymssql(_FakeClient()), optional=pyodbc)
    sqlserver._pyodbc_preferred = True

    assert sqlserver.use_pyodbc(_row()) is True
    opened = sqlserver.connect(_row(), "s3nh@")

    assert opened is client
    connection_string = pyodbc.connection_strings[-1]
    assert "ODBC Driver 18 for SQL Server" in connection_string
    assert "SERVER=sql.prod.local,1433" in connection_string
    assert "TrustServerCertificate=yes" in connection_string
    assert "TIMEOUT=5" in connection_string
    assert client.timeout == 8


def test_options_can_force_each_path():
    pymssql = _FakePymssql(_FakeClient())
    pyodbc = _FakePyodbc(_FakeClient())
    sqlserver.set_driver_module(pymssql, optional=pyodbc)

    asserted = sqlserver.connect(_row(options={"driver": "pymssql"}), "pw")
    assert asserted is pymssql.client
    other = sqlserver.connect(_row(options={"driver": "pyodbc"}), "pw")
    assert other is pyodbc.client


# --------------------------------------------------------------------------
# 1.5 SET TRANSACTION READ ONLY envolvendo a execução
# --------------------------------------------------------------------------


def test_execute_declares_read_only_before_the_statement():
    client = _FakeClient(rows=[("a", 1), ("b", 2)])
    sqlserver.set_driver_module(_FakePymssql(client), optional=None)

    result = sqlserver.execute(client, "SELECT * FROM t", 200)

    assert client.executed[0] == sqlserver.READ_ONLY_STATEMENT
    assert client.executed[-1] == "SELECT * FROM t"
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert result["server_version"] == "16.0.1000"
    assert client.rollbacks == 1  # transação revertida ao final


def test_execute_limits_rows_and_flags_truncation():
    client = _FakeClient(rows=[(i,) for i in range(4)])
    sqlserver.set_driver_module(_FakePymssql(client), optional=None)

    result = sqlserver.execute(client, "SELECT * FROM big", 2)

    assert result["rows"] == [[0], [1]]
    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_execute_reverts_the_transaction_even_on_error():
    client = _FakeClient(raises_on="SELECT")
    sqlserver.set_driver_module(_FakePymssql(client), optional=None)

    with pytest.raises(RuntimeError):
        sqlserver.execute(client, "SELECT ... DELETE FROM t", 200)

    assert client.rollbacks == 1, "a transação READ ONLY não pode ficar aberta"
    assert client.cursors_closed == 1


def test_execute_applies_statement_timeout_when_given():
    client = _FakeClient()
    sqlserver.set_driver_module(_FakePymssql(client), optional=None)

    sqlserver.execute(client, "SELECT 1", 10, statement_timeout_s=12)

    assert client.timeout == 12


def test_probe_and_close():
    client = _FakeClient(rows=[(1,)])
    sqlserver.set_driver_module(_FakePymssql(client), optional=None)

    assert sqlserver.probe(client) == {"server_version": "16.0.1000"}
    assert client.executed == [sqlserver.PROBE_STATEMENT]
    sqlserver.close(client)
    assert client.closed is True


# --------------------------------------------------------------------------
# integração com test_connection
# --------------------------------------------------------------------------


def test_probe_agrees_with_test_connection_server_version():
    """O ``probe`` uniforme reporta a mesma versão que ``test_connection``."""
    client = _FakeClient(rows=[(1,)], version="16.0.1000")
    pymssql = _FakePymssql(client)
    registry = DriverRegistry({"sqlserver": pymssql})

    result = service.test_connection(_row(), registry, lambda _name: "s3nh@")

    assert result["ok"] is True, result
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert result["server_version"] == "16.0.1000"
    assert sqlserver.probe(client)["server_version"] == result["server_version"]
