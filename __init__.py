"""Hermes plugin package for the database-connector plugin.

Everything the plugin offers the agent lives in :mod:`db_tools` (tasks 3.1–3.4:
the three tools, their read policy and the wrapper that never lets an exception
escape). ``register`` only wires the host into it: store, config provider,
secret resolver, emitter and finally the three ``ctx.register_tool`` calls.

Task P1b: the embedded skill is registered here too — one
``ctx.register_skill(<dir>, <dir>/SKILL.md)`` per ``skills/*/SKILL.md`` shipped
with the plugin (format of the embedded skills, section 9 of
``docs/research/hermes-plugin/01-criando-plugins.md``).

Every import is **lazy and inside the function** — a failure at import time (or
a host without the private helpers) would disable the plugin instead of just
leaving it without tools.
"""

from pathlib import Path

#: Skills shipped with the plugin: ``skills/<skill>/SKILL.md`` (task 4.1/P1b).
SKILLS_DIRNAME = "skills"
SKILL_FILENAME = "SKILL.md"


def skill_files(root: Path | None = None) -> list[tuple[str, Path]]:
    """``[(name, path)]`` of every embedded skill, sorted by name.

    Pure listing (no host involved): a missing directory simply yields nothing,
    so registration never fails for an incomplete checkout.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent
    skills = base / SKILLS_DIRNAME
    if not skills.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for path in sorted(skills.glob(f"*/{SKILL_FILENAME}")):
        if path.is_file():
            found.append((path.parent.name, path))
    return found


def register_skills(ctx: object, root: Path | None = None) -> list[str]:
    """Register every embedded skill — one failure never kills the plugin.

    Each ``register_skill`` is isolated: a skill the host refuses (name already
    taken, for instance) only drops itself, and the tools keep working.
    """
    registered: list[str] = []
    for name, path in skill_files(root):
        try:
            register = getattr(ctx, "register_skill", None)
            if not callable(register):
                break
            register(name, str(path))
        except Exception:  # noqa: BLE001 - uma skill nao derruba as outras
            continue
        registered.append(name)
    return registered


def register(ctx):
    """Entry point called by the Hermes plugin loader.

    Registro real em ctx do Hermes pendente (hostgator): só o aceite no host
    (``hermes plugins validate`` + uma chamada real do agente) confirma o
    contrato; aqui o wiring é o mesmo, com as tools já cobertas por testes.
    """
    register_skills(ctx)
    try:
        from db_tools import register_tools  # import lazy: ver docstring do módulo.
    # NB: o módulo NÃO se chama ``tools`` — o próprio Hermes tem um pacote
    # raiz ``tools/`` (registry de tools built-in) e o capability probe do
    # ``hermes plugins validate`` injeta a raiz do Hermes no PYTHONPATH à
    # frente do plugin; ``from tools import ...`` importaria o pacote do
    # host e o plugin carregaria com zero tools, silenciosamente.

        return register_tools(ctx)
    except Exception:  # noqa: BLE001 - registrar não pode derrubar o host
        return None
