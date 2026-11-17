"""Hermes plugin package for the database-connector plugin.

Phase 0 skeleton: ``register`` is a deliberate no-op. The service layer, the
REST routes and the agent tools arrive in the following changes, so nothing is
imported here yet (an import error at load time would disable the plugin).
"""


def register(ctx):
    """Entry point called by the Hermes plugin loader.

    Intentionally empty in this change — see ``add-plugin-backend`` task 1.2.
    """
    return None
