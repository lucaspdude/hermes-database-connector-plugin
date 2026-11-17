"""Task 1.4 — ``drivers/redis.py``: mesma interface, valor truncado em 64 KiB.

Every test injects a **fake** redis-py (the real driver is not installed in this
repo): no server is contacted. The validation against a real Redis instance is
still pending (local/Docker + host ``hostgator``).
"""

import pytest

from drivers import DriverMissingError
from drivers import redis as redis_driver


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, value=None, *, raises=None, version="7.2.4", **kwargs):
        self.kwargs = kwargs
        self.value = value
        self.raises = raises
        self.info_value = {"redis_version": version}
        self.calls: list[tuple] = []
        self.pings = 0
        self.closed = False

    # Execute semantics of redis-py: execute_command("GET", "chave").
    def execute_command(self, name, *args):
        self.calls.append((name, args))
        if self.raises is not None:
            raise self.raises
        return self.value

    def ping(self):
        self.pings += 1
        return True

    def close(self):
        self.closed = True

    @property
    def info(self):
        return self.info_value


class _FakeRedisModule:
    def __init__(self, client):
        self.client = client
        self.Redis = self._factory

    def _factory(self, **kwargs):
        self.last_kwargs = kwargs
        self.client.kwargs = kwargs
        return self.client


@pytest.fixture
def client():
    fake = _FakeClient(value="valor")
    module = _FakeRedisModule(fake)
    redis_driver.set_driver_module(module)
    yield fake
    redis_driver.forget_driver_module()


def _row(**overrides):
    payload = {
        "id": "cache",
        "engine": "redis",
        "environment": "prod",
        "host": "redis.prod",
        "port": 6379,
        "database": "2",
        "secret_ref": "DB_CACHE_PASSWORD",
        "options": {
            "db_index": 5,
            "tls": True,
            "connect_timeout_s": 4,
            "statement_timeout_s": 9,
            "socket_timeout_s": 3,
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------


def test_connect_usa_db_index_tls_e_timeouts_das_opcoes(client):
    redis_driver.connect(_row(), "segredo")

    kwargs = client.kwargs
    assert kwargs["host"] == "redis.prod"
    assert kwargs["port"] == 6379
    assert kwargs["db"] == 5  # db_index vence o `database` da conexao
    assert kwargs["ssl"] is True
    assert kwargs["socket_connect_timeout"] == 4
    assert kwargs["socket_timeout"] == 3  # socket_timeout_s explicito
    assert "username" not in kwargs  # a conexao nao declara usuario
    assert kwargs["password"] == "segredo"


def test_connect_defaults_quando_opcoes_ausentes(client):
    redis_driver.connect({"host": "localhost", "port": 6380}, None)

    kwargs = client.kwargs
    assert kwargs["socket_connect_timeout"] == redis_driver.DEFAULT_CONNECT_TIMEOUT_S
    assert kwargs["socket_timeout"] == redis_driver.DEFAULT_STATEMENT_TIMEOUT_S
    assert "ssl" not in kwargs  # TLS nunca e inventado
    assert "db" not in kwargs


def test_connect_sem_driver_reporta_driver_missing():
    redis_driver.forget_driver_module()
    redis_driver.set_driver_module(None)
    try:
        with pytest.raises(DriverMissingError):
            redis_driver._driver()
    finally:
        redis_driver.forget_driver_module()


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------


def test_execute_devolve_valor_e_duracao(client):
    fake = _FakeClient(value="valor da chave")
    module = _FakeRedisModule(fake)
    redis_driver.set_driver_module(module)

    result = redis_driver.execute(fake, "get", ["minha:chave"])

    assert result["value"] == "valor da chave"
    assert result["command"] == "GET"  # normalizado
    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert isinstance(result["duration_ms"], int)
    assert fake.calls == [("GET", ("minha:chave",))]


def test_execute_trunca_valor_em_64kib(client):
    big = "x" * (redis_driver.VALUE_MAX_BYTES + 1024)
    fake = _FakeClient(value=big)
    redis_driver.set_driver_module(_FakeRedisModule(fake))

    result = redis_driver.execute(fake, "GET", ["grande"])

    assert result["truncated"] is True
    assert len(result["value"].encode("utf-8")) == redis_driver.VALUE_MAX_BYTES


def test_execute_valor_abaixo_do_limite_nao_e_truncado(client):
    value = "y" * 1000
    fake = _FakeClient(value=value)
    redis_driver.set_driver_module(_FakeRedisModule(fake))

    result = redis_driver.execute(fake, "GET", ["pequena"])

    assert result["truncated"] is False
    assert result["value"] == value


def test_execute_decodifica_bytes_e_conta_itens(client):
    fake = _FakeClient(value={b"a": b"1", b"b": b"2"})
    redis_driver.set_driver_module(_FakeRedisModule(fake))

    result = redis_driver.execute(fake, "HGETALL", ["hash"])

    assert result["value"] == {"a": "1", "b": "2"}
    assert result["row_count"] == 2


def test_execute_sem_resultado_devolve_zero_linhas(client):
    fake = _FakeClient(value=None)
    redis_driver.set_driver_module(_FakeRedisModule(fake))

    result = redis_driver.execute(fake, "GET", ["ausente"])

    assert result["value"] is None
    assert result["row_count"] == 0


# --------------------------------------------------------------------------
# probe e close
# --------------------------------------------------------------------------


def test_probe_faz_ping(client):
    result = redis_driver.probe(client)

    assert client.pings == 1
    assert result == {"server_version": "7.2.4"}


def test_close_fecha_sem_levantar():
    class _Boom:
        def close(self):
            raise RuntimeError("nao fecha")

    class _SemClose:
        pass

    redis_driver.close(_Boom())  # nao levanta
    redis_driver.close(_SemClose())  # sem close(): tambem nao levanta


def test_shape_uniforme_com_as_outras_engines(client):
    """Mesmas chaves do postgres/sqlserver (o ciclo nao sabe a engine)."""
    result = redis_driver.execute(client, "LRANGE", ["lista", 0, -1])

    for key in ("row_count", "truncated", "duration_ms", "server_version"):
        assert key in result
    assert "value" in result
