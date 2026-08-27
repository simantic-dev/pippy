"""Python control of the Simantic simulators.

The firmware engine, hosted in your process.

    import simantic

    with simantic.Sim(elf="fw.elf", repl="board.repl") as sim:   # live control
        sim.expect("ready"); sim.run_for(0.5)
    run = simantic.run_firmware("fw.elf", repl="board.repl",
                                expect=["RESULT: PASS"])          # one-shot

The engine is fetched on first use. The `sim` CLI is optional; point
`$SIMANTIC_SIM` at one, or put it on PATH.

Installing this package also registers a pytest plugin that turns `test.yaml`
fixture manifests into individually addressable pytest items.
"""

from ._elf import symbols_in_file as elf_symbols
from ._locate import BinaryNotFound
from .fixtures import (
    Manifest,
    ModelLibraryUnavailable,
    UnsupportedManifest,
    load_manifest,
)
from .mcu import ServerNotConfigured, SimError, SimRun, sim_binary
from .mcu import run as run_firmware
from .engine import EngineNotFound, engine_dir, rust_engine_dir
from .platforms import list_platforms, read_platform, validate_platform
from ._replx import list_models
from .session import BACKENDS, ExpectTimeout, Match, Sim
from ._rust import NotSupported

__version__ = "0.3.1"

__all__ = [
    # firmware
    "Manifest",
    "ModelLibraryUnavailable",
    "ServerNotConfigured",
    "SimError",
    "SimRun",
    "UnsupportedManifest",
    "load_manifest",
    "run_firmware",
    "sim_binary",
    # scripted sessions
    "Sim",
    "Match",
    "ExpectTimeout",
    "EngineNotFound",
    "NotSupported",
    "BACKENDS",
    "engine_dir",
    "rust_engine_dir",
    # static inspection — no engine, no running firmware
    "elf_symbols",
    "list_platforms",
    "read_platform",
    "validate_platform",
    "list_models",
    # shared
    "BinaryNotFound",
]
