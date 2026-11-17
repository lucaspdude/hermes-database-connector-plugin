import pathlib

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin.yaml"
PREFIX = "/api/plugins/database-connector"


def test_manifest_parses_with_the_three_provided_tools():
    assert MANIFEST.exists(), "plugin.yaml must live at the repo root"

    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert data["name"] == "database-connector"
    # Task 3.5: the manifest mirrors exactly what register_tools registers.
    import tools

    assert sorted(data["provides_tools"]) == sorted(tools.TOOL_NAMES)


def test_ping_route_mounted_on_plugin_prefix():
    from dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix=PREFIX)

    response = TestClient(app).get(f"{PREFIX}/ping")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["plugin"] == "database-connector"
