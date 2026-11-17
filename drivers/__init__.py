"""Detection and lazy loading of the per-engine Python drivers.

Design decision 7 (``add-plugin-backend``): availability is decided **without**
importing the driver (:func:`importlib.util.find_spec`), and the import happens
lazily, at execution time. Consequences:

* a missing driver never breaks the plugin load nor the other engines (R1);
* the error carries an actionable ``install`` command;
* the ``uv`` binary is resolved before mounting that command, because on the
  target host (``hostgator``, AlmaLinux 9) it is vendored under
  ``~/.hermes/tools/uv-*/uv`` and is **not** on ``PATH``.

Nothing here talks to a database: the probe logic lives in ``service.py``.
"""

from __future__ import annotations

import glob
import importlib
import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

#: Path fragment appended to the Hermes home to find the vendored uv binary.
UV_GLOB = os.path.join(".hermes", "tools", "uv-*", "uv")

#: Suggested command template (uv path already resolved).
INSTALL_TEMPLATE = "{uv} pip install --python {python} {package}"


@dataclass(frozen=True)
class DriverSpec:
    """One engine's driver: import name, pip requirement and probe kind."""

    engine: str
    module: str
    package: str
    #: "sql" for engines probed with ``SELECT 1``, "ping" for Redis.
    probe: str = "sql"


#: Mirrors ``plugin.yaml`` -> ``python_dependencies`` (upper bound mandatory).
DRIVER_SPECS: dict[str, DriverSpec] = {
    "sqlserver": DriverSpec("sqlserver", "pymssql", "pymssql>=2.3,<3", "sql"),
    "postgres": DriverSpec("postgres", "psycopg", "psycopg[binary]>=3.1,<4", "sql"),
    "redis": DriverSpec("redis", "redis", "redis>=5,<7", "ping"),
}

DRIVER_MISSING = "driver_missing"


class DriverMissingError(RuntimeError):
    """Raised when an engine has no usable driver in the current process.

    Never a bare ``ImportError``: callers turn this into a clean
    ``{"ok": false, "code": "driver_missing", "install": ...}`` payload.
    """

    code = DRIVER_MISSING

    def __init__(self, engine: str, module: str, install: str, detail: str | None = None):
        self.engine = engine
        self.module = module
        self.install = install
        self.detail = detail
        super().__init__(detail or f"driver '{module}' for engine '{engine}' is not installed")


def known_engines() -> tuple[str, ...]:
    return tuple(DRIVER_SPECS)


def spec_for(engine: str) -> DriverSpec:
    """Return the :class:`DriverSpec` of ``engine`` (raises on unknown engine)."""
    try:
        return DRIVER_SPECS[engine]
    except KeyError:
        raise ValueError(
            f"invalid engine: {engine!r}; expected one of {', '.join(known_engines())}"
        ) from None


# --------------------------------------------------------------------------
# resolução do caminho do uv  (design 7: hostgator não tem uv no PATH)
# --------------------------------------------------------------------------


def resolve_uv_path(
    home: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Return the uv binary path: vendored glob first, then PATH, then ``"uv"``.

    ``home`` and ``which`` are injectable so tests do not depend on the real
    filesystem/PATH. The final fallback is the plain name ``"uv"`` — the command
    stays valid where uv is installed system-wide or resolved by a shell.
    """
    base = home if home is not None else os.path.expanduser("~")
    matches = sorted(
        path for path in glob.glob(os.path.join(base, UV_GLOB)) if os.path.isfile(path)
    )
    if matches:
        return matches[-1]

    finder = which if which is not None else shutil.which
    try:
        found = finder("uv")
    except Exception:  # noqa: BLE001 - PATH lookup must never break health
        found = None
    return found or "uv"


def install_command(
    engine: str,
    *,
    home: str | None = None,
    which: Callable[[str], str | None] | None = None,
    python_executable: str | None = None,
    uv_path: str | None = None,
) -> str:
    """Ready-to-run install command for ``engine`` (uv path already resolved)."""
    spec = spec_for(engine)
    uv = uv_path or resolve_uv_path(home=home, which=which)
    python = python_executable or sys.executable
    return INSTALL_TEMPLATE.format(uv=uv, python=python, package=spec.package)


# --------------------------------------------------------------------------
# detecção sem importar
# --------------------------------------------------------------------------


def detect_module(
    module_name: str,
    finder: Callable[[str], Any | None] | None = None,
) -> bool:
    """True when ``module_name`` is importable — **without** importing it.

    ``find_spec`` may raise for a broken/partially installed distribution; that
    counts as absent, never as an exception escaping to the caller (R1).
    """
    finder = finder or importlib.util.find_spec
    try:
        return finder(module_name) is not None
    except Exception:  # noqa: BLE001 - ModuleNotFoundError, ValueError, ...
        return False


class DriverRegistry:
    """Per-engine view of driver availability + lazy loader.

    ``overrides`` injects the driver (for tests and fakes): a truthy value is
    used as-is and marks the engine available; ``None`` forces it missing.
    """

    def __init__(
        self,
        overrides: Mapping[str, Any] | None = None,
        *,
        home: str | None = None,
        which: Callable[[str], str | None] | None = None,
        finder: Callable[[str], Any | None] | None = None,
        importer: Callable[[str], Any] | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.overrides = dict(overrides or {})
        self.home = home
        self._which = which
        self._finder = finder
        self._importer = importer or importlib.import_module
        self._python_executable = python_executable or sys.executable

    # -- install sugerido --------------------------------------------------

    def uv_path(self) -> str:
        return resolve_uv_path(home=self.home, which=self._which)

    def install_command(self, engine: str) -> str:
        spec = spec_for(engine)
        return INSTALL_TEMPLATE.format(
            uv=self.uv_path(), python=self._python_executable, package=spec.package
        )

    # -- disponibilidade (nunca importa) ------------------------------------

    def has_override(self, engine: str) -> bool:
        return spec_for(engine).engine in self.overrides

    def is_available(self, engine: str) -> bool:
        """Available = injected fake/driver, else detected by ``find_spec``."""
        spec = spec_for(engine)
        if spec.engine in self.overrides:
            return self.overrides[spec.engine] is not None
        return detect_module(spec.module, self._finder)

    def status(self, engine: str) -> dict[str, Any]:
        """``{ available, module, install }`` for one engine (spec backend-api)."""
        spec = spec_for(engine)
        return {
            "available": self.is_available(engine),
            "module": spec.module,
            "install": self.install_command(engine),
        }

    def health(self, engines: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
        """Per-engine availability; one absent engine never affects the others."""
        engines = tuple(engines) if engines is not None else known_engines()
        health: dict[str, dict[str, Any]] = {}
        for engine in engines:
            try:
                health[engine] = self.status(engine)
            except Exception as exc:  # noqa: BLE001 - isolate per engine (R1)
                health[engine] = {
                    "available": False,
                    "module": DRIVER_SPECS[engine].module if _engine_known(engine) else "",
                    "install": "",
                    "error": str(exc),
                }
        return health

    # -- carregamento lazy --------------------------------------------------

    def load(self, engine: str) -> Any:
        """Return the driver module for ``engine`` (import happens here).

        Raises :class:`DriverMissingError` — never a raw ``ImportError`` — when
        the engine has no driver, so no stack trace ever reaches the caller.
        """
        spec = spec_for(engine)
        install = self.install_command(engine)

        if spec.engine in self.overrides:
            injected = self.overrides[spec.engine]
            if injected is None:
                raise DriverMissingError(
                    engine, spec.module, install,
                    f"driver '{spec.module}' is not available for engine '{engine}'",
                )
            return injected

        if not detect_module(spec.module, self._finder):
            raise DriverMissingError(
                engine, spec.module, install,
                f"driver '{spec.module}' is not installed for engine '{engine}'",
            )
        try:
            return self._importer(spec.module)
        except Exception as exc:  # noqa: BLE001 - broken install counts as missing
            raise DriverMissingError(
                engine, spec.module, install,
                f"driver '{spec.module}' for engine '{engine}' could not be imported "
                f"({type(exc).__name__})",
            ) from None


def _engine_known(engine: str) -> bool:
    return engine in DRIVER_SPECS


#: Registry used when no injection is needed (real host, real detection).
def default_registry() -> DriverRegistry:
    return DriverRegistry()
