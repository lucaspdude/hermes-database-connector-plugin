"""Tests da task 4.5 (``GET /health``): health de drivers por engine.

Nenhum driver real e importado - o registry e sempre injetado com fakes. O host
completo e o host com um driver faltando sao simulados via ``overrides``; a
regra "detectar sem importar" e verificada com um ``importer`` que falharia se
fosse chamado. **curl num host real (hostgator) segue pendente.**
"""

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import plugin_api
from drivers import DRIVER_SPECS, DriverRegistry

PREFIX = "/api/plugins/database-connector"
FAKE_UV = "/opt/hermes/tools/uv-0.4.20/uv"
#: home inexistente: garante que o glob vendored nao ache nada no host de teste
#: e o caminho do uv venha do ``which`` injetado (deterministico).
EMPTY_HOME = "/nonexistent-hermes-home-for-tests"
ENGINES = ("sqlserver", "postgres", "redis")


class _FakeDriver:
    """Stand-in de driver: nunca e chamado pelo health (so marcado presente)."""

    def __init__(self, engine):
        self.engine = engine


def _registry(overrides=None, **kwargs):
    """Registry de teste: uv resolvido por ``which`` (sem tocar o PATH real)."""
    return DriverRegistry(
        overrides or {}, home=EMPTY_HOME, which=lambda _name: FAKE_UV, **kwargs
    )


@pytest.fixture()
def app():
    """App montada so com o router (health nao usa store)."""

    def mount(registry):
        plugin_api.set_driver_registry(registry)
        application = FastAPI()
        plugin_api.install_error_handlers(application)
        application.include_router(plugin_api.router, prefix=PREFIX)
        return TestClient(application)

    yield mount
    plugin_api.set_driver_registry(None)


# --------------------------------------------------------------------------
# host completo: tres engines available
# --------------------------------------------------------------------------


def test_health_reports_every_engine_available_on_a_complete_host(app):
    client = app(_registry({engine: _FakeDriver(engine) for engine in ENGINES}))

    response = client.get(f"{PREFIX}/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert set(body["engines"]) == set(ENGINES)

    for engine in ENGINES:
        entry = body["engines"][engine]
        assert entry["available"] is True, entry
        assert entry["module"] == DRIVER_SPECS[engine].module
        # comando pronto: uv resolvido + pacote com faixa superior obrigatoria
        assert entry["install"] == (
            f"{FAKE_UV} pip install --python {sys.executable} {DRIVER_SPECS[engine].package}"
        )
        assert "Traceback" not in response.text


def test_health_install_command_carries_the_resolved_uv_path(app):
    """O caminho do uv vem do glob vendored/PATH - nunca um ``uv`` presumido."""
    client = app(_registry({engine: _FakeDriver(engine) for engine in ENGINES}))

    installs = client.get(f"{PREFIX}/health").json()["engines"]

    for entry in installs.values():
        assert entry["install"].startswith(f"{FAKE_UV} pip install --python ")
        assert "pymssql>=2.3,<3" in installs["sqlserver"]["install"]
        assert "psycopg[binary]>=3.1,<4" in installs["postgres"]["install"]
        assert "redis>=5,<7" in installs["redis"]["install"]


# --------------------------------------------------------------------------
# host sem um driver: so ele fica indisponivel, comando acionavel
# --------------------------------------------------------------------------


def test_missing_driver_marks_only_its_engine_and_keeps_the_others(app):
    overrides = {
        "sqlserver": None,  # driver ausente
        "postgres": _FakeDriver("postgres"),
        "redis": _FakeDriver("redis"),
    }
    client = app(_registry(overrides))

    engines = client.get(f"{PREFIX}/health").json()["engines"]

    missing = engines["sqlserver"]
    assert missing["available"] is False
    assert missing["module"] == "pymssql"
    # comando acionavel: da para copiar e colar no host
    assert missing["install"] == (
        f"{FAKE_UV} pip install --python {sys.executable} pymssql>=2.3,<3"
    )
    # as demais seguem disponiveis
    assert engines["postgres"]["available"] is True
    assert engines["redis"]["available"] is True


def test_health_survives_two_missing_drivers(app):
    client = app(_registry({"sqlserver": None, "postgres": None, "redis": _FakeDriver("redis")}))

    engines = client.get(f"{PREFIX}/health").json()["engines"]

    assert engines["sqlserver"]["available"] is False
    assert engines["postgres"]["available"] is False
    assert engines["redis"]["available"] is True


# --------------------------------------------------------------------------
# nenhuma engine e importada durante o health (so find_spec)
# --------------------------------------------------------------------------


def test_health_never_imports_the_drivers(app):
    """``importer`` estouraria se o health importasse - ``find_spec`` e o unico."""
    imported: list[str] = []

    def importer(name):  # pragma: no cover - chamado so em caso de regressao
        imported.append(name)
        raise AssertionError(f"driver '{name}' nao deveria ser importado no health")

    def finder(name):
        # qualquer objeto nao-None conta como "modulo encontrado"
        return object()

    client = app(_registry({}, importer=importer, finder=finder))

    engines = client.get(f"{PREFIX}/health").json()["engines"]

    assert imported == []
    assert all(entry["available"] for entry in engines.values())
    assert {DRIVER_SPECS[engine].module for engine in ENGINES} == {
        entry["module"] for entry in engines.values()
    }


def test_health_does_not_import_when_a_driver_is_missing(app):
    imported: list[str] = []

    def importer(name):  # pragma: no cover
        imported.append(name)
        raise AssertionError(f"driver '{name}' nao deveria ser importado no health")

    def finder(name):
        return object()

    client = app(_registry({"redis": None}, importer=importer, finder=finder))

    body = client.get(f"{PREFIX}/health").json()

    assert imported == []
    assert body["engines"]["redis"]["available"] is False
    assert body["engines"]["redis"]["install"]


def test_health_answers_error_as_error_and_detail(app):
    """Erro inesperado no health tambem sai como {error, detail}, sem traceback."""

    class BrokenRegistry:
        def health(self, *_args, **_kwargs):
            raise RuntimeError("registry quebrado: dsn=postgres://u:s3nh@h/db")

    client = app(BrokenRegistry())

    response = client.get(f"{PREFIX}/health")

    assert response.status_code == 500, response.text
    body = response.json()
    assert set(body) == {"error", "detail"}
    assert "s3nh" not in response.text and "postgres://" not in response.text
    assert "Traceback" not in response.text
