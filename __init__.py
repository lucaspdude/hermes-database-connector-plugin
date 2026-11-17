"""Hermes plugin package for the database-connector plugin.

Everything the plugin offers the agent lives in :mod:`tools` (tasks 3.1–3.4:
the three tools, their read policy and the wrapper that never lets an exception
escape). ``register`` only wires the host into it: store, config provider,
secret resolver, emitter and finally the three ``ctx.register_tool`` calls.

Every import is **lazy and inside the function** — a failure at import time (or
a host without the private helpers) would disable the plugin instead of just
leaving it without tools.
"""


def register(ctx):
    """Entry point called by the Hermes plugin loader.

    Registro real em ctx do Hermes pendente (hostgator): só o aceite no host
    (``hermes plugins validate`` + uma chamada real do agente) confirma o
    contrato; aqui o wiring é o mesmo, com as tools já cobertas por testes.
    """
    try:
        from tools import register_tools  # import lazy: ver docstring do módulo

        return register_tools(ctx)
    except Exception:  # noqa: BLE001 - registrar não pode derrubar o host
        return None
