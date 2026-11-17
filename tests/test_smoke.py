import pathlib

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin.yaml"
PREFIX = "/api/plugins/database-connector"


def test_manifest_parses_with_empty_provides_tools():
    assert MANIFEST.exists(), "plugin.yaml must live at the repo root"

    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert data["name"] == "database-connector"
    assert data["provides_tools"] == []


def test_ping_route_mounted_on_plugin_prefix():
    from dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix=PREFIX)

    response = TestClient(app).get(f"{PREFIX}/ping")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["plugin"] == "database-connector"
