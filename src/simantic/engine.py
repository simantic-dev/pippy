"""Loading the simulation engine into this process.

`Simantic.Core` is a .NET library; `sim` is one face of it and this package is
another. `simantic install` (or `$SIMANTIC_SIM`) gives us the directory that
holds `sim`, `Simantic.Core.dll` and the runtime config, which is everything
needed to host the engine here via pythonnet. Nothing is spawned: the Python
objects *are* the session.

One emulation per process — the engine keeps process-global state — so
parallel runs are parallel processes (`ProcessPoolExecutor`).
"""

from __future__ import annotations

import os
import sys
from functools import cache
from pathlib import Path

from ._locate import BinaryNotFound
from .mcu import sim_binary

ENV_DIR = "SIMANTIC_ENGINE_DIR"


class EngineNotFound(RuntimeError):
    """The engine assemblies could not be located or loaded."""


def engine_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The directory holding Simantic.Core.dll and sim.runtimeconfig.json."""
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    if os.environ.get(ENV_DIR):
        candidates.append(Path(os.environ[ENV_DIR]))
    try:
        candidates.append(sim_binary().resolve().parent)
    except BinaryNotFound:
        pass
    for d in candidates:
        if (d / "Simantic.Core.dll").exists() and (d / "sim.runtimeconfig.json").exists():
            return d
    raise EngineNotFound(
        "Simantic.Core.dll not found. Run `simantic install`, or point $SIMANTIC_SIM at a sim "
        f"binary inside its publish directory, or set ${ENV_DIR}."
    )


@cache
def load(explicit: str | os.PathLike[str] | None = None):
    """Host the .NET runtime and import Simantic.Core. Returns the Session namespace."""
    d = engine_dir(explicit)
    try:
        from pythonnet import load as load_runtime
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise EngineNotFound("pythonnet is required to host the engine: pip install pythonnet") from exc
    load_runtime("coreclr", runtime_config=str(d / "sim.runtimeconfig.json"))
    import clr  # noqa: F401  (provided by pythonnet after load)

    if str(d) not in sys.path:
        sys.path.append(str(d))
    clr.AddReference("Simantic.Core")
    import Simantic.Core.Emulation.Session as session_ns  # type: ignore[import-not-found]

    return session_ns
