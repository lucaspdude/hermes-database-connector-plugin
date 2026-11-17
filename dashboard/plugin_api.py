"""HTTP adapter for the database-connector plugin (dashboard half).

Phase 0 skeleton: only the ping route exists so the mount
``/api/plugins/database-connector/`` can be verified end to end. The real
routes are added by the ``add-plugin-backend`` REST tasks as thin adapters over
``service.py``.
"""

from fastapi import APIRouter

PLUGIN_ID = "database-connector"

router = APIRouter()


@router.get("/ping")
def ping():
    return {"ok": True, "plugin": PLUGIN_ID}
