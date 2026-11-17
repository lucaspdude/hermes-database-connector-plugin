"""Unit tests for 3.1 ``test_connection`` and 3.2 driver detection/health.

Real drivers are **not** installed here (they land in the next change): every
engine is exercised with an injected fake, exactly like the REST layer will do
with the registry from ``drivers/``. The probe against a real instance is still
pending (host ``hostgator``).
"""

import json
import os
import sys

import pytest

import drivers
import service


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
        "options": {"connect_timeout_s": 5, "statement_timeout_s": 5},
    }
    payload.update(overrides)
    return payload


def resolver(_name):
    return "s3nh@SuperSecreta"


# --------------------------------------------------------------------------
# fakes de driver (injecao pelo registry)
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, client):
        self.client = client

    def execute(self, statement):
        self.client.executed.append(statement)
        if self.client.probe_delay:
            import time

            time.sleep(self.client.probe_delay)

    def fetchone(self):
        return (1,)

    def close(self):
        self.client.cursor_closed = True


class _FakeSqlClient:
    def __init__(self, *, probe_delay=0.0, version="16.0.1000"):
        self.probe_delay = probe_delay
        self.server_version = version
        self.executed: list[str] = []
        self.cursor_closed = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


class _FakeSqlDriver:
    """Minimal stand-in for ``pymssql``/``psycopg``."""

    def __init__(self, *, probe_delay=0.0, connect_delay=0.0, version="16.0.1000", raises=None):
        self.probe_delay = probe_delay
        self.connect_delay = connect_delay
        self.version = version
        self.raises = raises
        self.connect_kwargs: dict = {}
        self.clients: list[_FakeSqlClient] = []

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_delay:
            import time

            time.sleep(self.connect_delay)
        if self.raises:
            raise self.raises
        client = _FakeSqlClient(probe_delay=self.probe_delay, version=self.version)
        self.clients.append(client)
        return client


class _FakeRedisClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pings = 0
        self.closed = False

    def ping(self):
        self.pings += 1
        return True

    def close(self):
        self.closed = True


class _FakeRedisDriver:
    """Minimal stand-in for ``redis``."""

    def __init__(self):
        self.clients: list[_FakeRedisClient] = []

    def Redis(self, **kwargs):
        client = _FakeRedisClient(**kwargs)
        self.clients.append(client)
        return client


# --------------------------------------------------------------------------
# 3.1 test_connection
# --------------------------------------------------------------------------


def test_test_connection_opens_probes_closes_and_returns_latency():
    driver = _FakeSqlDriver(version="16.0.1000")
    registry = drivers.DriverRegistry({"sqlserver": driver})

    result = service.test_connection(_connection(), registry, resolver)

    assert result["ok"] is True, result
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert result["server_version"] == "16.0.1000"

    # abriu -> sondou -> fechou
    assert driver.connect_kwargs["server"] == "sql.prod.local:1433"
    assert driver.connect_kwargs["database"] == "HubFinanceira"
    assert driver.clients[0].executed == ["SELECT 1"]
    assert driver.clients[0].cursor_closed is True
    assert driver.clients[0].closed is True

    # sem vazamento: stack trace, DSN completo ou senha
    blob = json.dumps(result, ensure_ascii=False)
    assert "Traceback" not in blob
    assert resolver("x") not in blob


def test_redis_probe_pings_instead_of_selecting():
    driver = _FakeRedisDriver()
    registry = drivers.DriverRegistry({"redis": driver})

    result = service.test_connection(
        _connection(engine="redis", host="cache.local", port=6379, database="0"),
        registry,
        resolver,
    )

    assert result["ok"] is True, result
    assert driver.clients[0].pings == 1
    assert driver.clients[0].closed is True
    assert driver.clients[0].kwargs["socket_connect_timeout"] == 5


def test_unresolved_secret_fails_cleanly_without_exposing_the_value():
    driver = _FakeSqlDriver()
    registry = drivers.DriverRegistry({"sqlserver": driver})

    result = service.test_connection(_connection(), registry, resolver=lambda _name: "")

    assert result["ok"] is False
    assert result["code"] == "secret_missing"
    assert result["secret_state"] == service.STATE_MISSING
    assert driver.connect_kwargs == {}  # nunca abriu
    blob = json.dumps(result, ensure_ascii=False)
    assert "s3nh@" not in blob and "Traceback" not in blob


def test_missing_driver_returns_driver_missing_with_install_command(tmp_path):
    fake_uv = tmp_path / ".hermes" / "tools" / "uv-9.9.9" / "uv"
    fake_uv.parent.mkdir(parents=True)
    fake_uv.write_text("#!/bin/sh\n", encoding="utf-8")

    registry = drivers.DriverRegistry({"postgres": None}, home=str(tmp_path))
    row = _connection(
        engine="postgres", host="pg.local", port=5432, database="hub", username="ro"
    )

    result = service.test_connection(row, registry, resolver)

    assert result["ok"] is False
    assert result["code"] == drivers.DRIVER_MISSING
    assert result["module"] == "psycopg"
    assert result["install"] == (
        f"{fake_uv} pip install --python {sys.executable} psycopg[binary]>=3.1,<4"
    )
    assert "Traceback" not in result["error"]


def test_probe_respects_statement_timeout_without_raising():
    driver = _FakeSqlDriver(probe_delay=0.5)
    registry = drivers.DriverRegistry({"sqlserver": driver})
    row = _connection(options={"connect_timeout_s": 5, "statement_timeout_s": 0.05})

    result = service.test_connection(row, registry, resolver)

    assert result["ok"] is False
    assert result["code"] == "statement_timeout"
    assert result["latency_ms"] >= 0
    assert isinstance(result["error"], str) and result["error"]


def test_connect_failure_is_reported_without_stack_trace_or_password():
    driver = _FakeSqlDriver(raises=RuntimeError("login failed for user 'hermes_ro' host sql.prod.local"))
    registry = drivers.DriverRegistry({"sqlserver": driver})

    result = service.test_connection(_connection(), registry, resolver)

    assert result["ok"] is False
    assert result["code"] == "connect_error"
    assert "RuntimeError" in result["error"]
    password = resolver("x")
    assert password not in result["error"]
    assert result["error"] == result["error"].splitlines()[0]  # uma linha, sem traceback


def test_timeouts_default_to_ten_and_thirty_seconds():
    row = _connection()
    row.pop("options")
    row["options_json"] = "{}"

    assert service.connection_timeouts(row) == (
        service.DEFAULT_CONNECT_TIMEOUT_S,
        service.DEFAULT_STATEMENT_TIMEOUT_S,
    )
    # options como string JSON tambem valem
    row["options_json"] = json.dumps({"connect_timeout_s": 1, "statement_timeout_s": 2})
    assert service.connection_timeouts(row) == (1.0, 2.0)


# --------------------------------------------------------------------------
# 3.2 detecção por find_spec, health por engine e caminho do uv
# --------------------------------------------------------------------------


def test_missing_engine_does_not_affect_the_others():
    registry = drivers.DriverRegistry(
        {"sqlserver": None, "postgres": _FakeSqlDriver(), "redis": _FakeRedisDriver()}
    )

    health = registry.health()

    assert set(health) == {"sqlserver", "postgres", "redis"}
    assert health["sqlserver"]["available"] is False
    assert health["sqlserver"]["module"] == "pymssql"
    assert health["sqlserver"]["install"].endswith("pymssql>=2.3,<3")
    assert health["postgres"]["available"] is True
    assert health["postgres"]["module"] == "psycopg"
    assert health["redis"]["available"] is True
    assert health["redis"]["module"] == "redis"

    # a engine sem driver continua testável sem derrubar o registry
    result = service.test_connection(_connection(), registry, resolver)
    assert result["ok"] is False and result["code"] == drivers.DRIVER_MISSING


def test_real_detection_never_imports_and_covers_every_engine(monkeypatch):
    imported: list[str] = []

    def fail_import(name, *args, **kwargs):  # pragma: no cover - guard
        imported.append(name)
        raise AssertionError("health must not import the driver")

    monkeypatch.setattr(drivers.importlib, "import_module", fail_import)
    monkeypatch.setattr(
        drivers.importlib.util,
        "find_spec",
        # psycopg presente, os demais ausentes
        lambda name: object() if name == "psycopg" else None,
    )

    health = drivers.default_registry().health()

    assert imported == []
    assert health["postgres"]["available"] is True
    assert health["sqlserver"]["available"] is False
    assert health["redis"]["available"] is False
    assert all(entry["install"] for entry in health.values())


def test_registry_reports_unknown_module_as_missing_not_as_crash():
    def broken_find_spec(_name):
        raise ValueError("bad distribution metadata")

    registry = drivers.DriverRegistry(finder=broken_find_spec)

    assert registry.is_available("sqlserver") is False
    assert registry.health()["redis"]["available"] is False

    with pytest.raises(drivers.DriverMissingError) as excinfo:
        registry.load("redis")

    assert excinfo.value.code == drivers.DRIVER_MISSING
    assert excinfo.value.install.endswith("redis>=5,<7")


def test_resolve_uv_path_prefers_the_vendored_glob(tmp_path):
    tool_dir = tmp_path / ".hermes" / "tools" / "uv-0.5.4"
    tool_dir.mkdir(parents=True)
    fake_uv = tool_dir / "uv"
    fake_uv.write_text("#!/bin/sh\n", encoding="utf-8")

    resolved = drivers.resolve_uv_path(home=str(tmp_path), which=lambda _name: None)

    assert resolved == str(fake_uv)
    assert os.path.isfile(resolved)


def test_resolve_uv_path_falls_back_to_uv_without_glob_or_path(tmp_path):
    resolved = drivers.resolve_uv_path(
        home=str(tmp_path / "empty-home"), which=lambda _name: None
    )

    assert resolved == "uv"
    assert (
        drivers.install_command(
            "sqlserver", home=str(tmp_path / "empty-home"), which=lambda _name: None
        )
        == f"uv pip install --python {sys.executable} pymssql>=2.3,<3"
    )
