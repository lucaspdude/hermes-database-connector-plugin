"""Task 1.1 — registry: lazy import happens **on load**, once, and is cached.

The registry of ``add-plugin-backend`` already detected and reported; this
covers what it must now guarantee:

* the engine module is imported only when ``load(engine)`` runs (never at
  plugin import time) and only on the first execution — later ones reuse it;
* an engine without a driver yields ``driver_missing`` **without** breaking the
  other engines (R1);
* the real-instance validation of each driver is still pending
  (host ``hostgator``: SQL Server/Redis external, Postgres local — none of the
  real ``psycopg``/``pymssql``/``pyodbc`` packages is installed here).
"""

import importlib
import sys
import types

import pytest

import drivers
from drivers import DriverMissingError


class _SentinelSpec:
    """Stands in for a ``find_spec`` result: truthy == module present."""

    def __init__(self, name):
        self.name = name


def _registry_present(module_name: str, calls: list[str]) -> drivers.DriverRegistry:
    """Registry where ``module_name`` is detected and imported for real."""

    def finder(name):
        return _SentinelSpec(name) if name == module_name else None

    def importer(name):
        calls.append(name)
        return importlib.import_module(name)

    return drivers.DriverRegistry(finder=finder, importer=importer)


# --------------------------------------------------------------------------
# import lazy + cache
# --------------------------------------------------------------------------


def test_load_imports_the_engine_module_only_on_first_execution(monkeypatch):
    fake = types.ModuleType("psycopg")
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    calls: list[str] = []
    registry = _registry_present("psycopg", calls)

    assert calls == []  # nothing imported before the first load
    first = registry.load("postgres")
    second = registry.load("postgres")
    third = registry.load("postgres")

    assert first is fake and second is fake and third is fake
    assert calls == ["psycopg"], "the driver must be imported once and cached"


def test_load_cache_is_per_engine_and_can_be_forgotten(monkeypatch):
    pg = types.ModuleType("psycopg")
    ms = types.ModuleType("pymssql")
    monkeypatch.setitem(sys.modules, "psycopg", pg)
    monkeypatch.setitem(sys.modules, "pymssql", ms)
    calls: list[str] = []

    def finder(name):
        return _SentinelSpec(name) if name in ("psycopg", "pymssql") else None

    def importer(name):
        calls.append(name)
        return importlib.import_module(name)

    registry = drivers.DriverRegistry(finder=finder, importer=importer)

    assert registry.load("postgres") is pg
    assert registry.load("sqlserver") is ms
    assert sorted(calls) == ["psycopg", "pymssql"]

    registry.forget("postgres")
    assert registry.load("postgres") is pg  # re-imported, same module
    assert calls.count("psycopg") == 2
    assert calls.count("pymssql") == 1  # the other engine keeps its cache


def test_registry_import_nothing_at_construction_time():
    """Constructing the registry never imports any engine module."""
    before = set(sys.modules)
    drivers.DriverRegistry()
    assert not {"psycopg", "pymssql", "redis"} & (set(sys.modules) - before)


# --------------------------------------------------------------------------
# engine sem driver nao quebra as outras
# --------------------------------------------------------------------------


def test_missing_engine_reports_driver_missing_without_breaking_the_others(monkeypatch):
    pg = types.ModuleType("psycopg")
    monkeypatch.setitem(sys.modules, "psycopg", pg)

    def finder(name):
        return _SentinelSpec(name) if name == "psycopg" else None

    registry = drivers.DriverRegistry(finder=finder)

    with pytest.raises(DriverMissingError) as excinfo:
        registry.load("sqlserver")
    assert excinfo.value.code == "driver_missing"
    assert "uv pip install" in excinfo.value.install

    # a ausente nao quebra quem tem driver...
    assert registry.load("postgres") is pg
    # ...nem a deteccao (health) das demais
    health = registry.health()
    assert health["postgres"]["available"] is True
    assert health["sqlserver"]["available"] is False


def test_broken_install_counts_as_missing_not_as_traceback(monkeypatch):
    """A module that raises on import is reported as missing (R1)."""

    def finder(name):
        return _SentinelSpec(name)

    def importer(_name):
        raise RuntimeError("shared object missing")

    registry = drivers.DriverRegistry(finder=finder, importer=importer)
    with pytest.raises(DriverMissingError):
        registry.load("redis")


def test_override_none_forces_missing_and_truthy_is_reused():
    fake = object()
    registry = drivers.DriverRegistry({"redis": fake, "postgres": None})

    assert registry.load("redis") is fake
    with pytest.raises(DriverMissingError):
        registry.load("postgres")
