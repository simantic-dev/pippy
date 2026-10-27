"""Python control of the Simantic simulators.

The `sim` binary runs batches; the engine can also be hosted in your process.

    import simantic

    with simantic.Sim(elf="fw.elf", mcu="STM32F401RE") as sim:   # live control
        sim.expect("ready"); sim.run_for(0.5)
    run = simantic.run_firmware("fw.elf", mcu="STM32F401RE",
                                expect=["RESULT: PASS"])          # one-shot

`run_firmware` and `run_scenario` need the `sim` binary: point `$SIMANTIC_SIM`
at one, or put it on PATH. `Sim` fetches its engine on first use.

Installing this package also registers a pytest plugin that turns `test.yaml`
fixture manifests into individually addressable pytest items.
"""

from ._locate import BinaryNotFound
from .fixtures import (
    Manifest,
    UnsupportedManifest,
    load_manifest,
)
from .mcu import ScenarioRun, SimError, SimRun, run_scenario, sim_binary
from .mcu import run as run_firmware
from .engine import EngineNotFound, rust_engine_dir
from .session import ExpectTimeout, Match, Sim
from ._rust import NotSupported

__version__ = "0.5.0"

__all__ = [
    # firmware
    "Manifest",
    "SimError",
    "SimRun",
    "UnsupportedManifest",
    "load_manifest",
    "run_firmware",
    "run_scenario",
    "ScenarioRun",
    "sim_binary",
    # scripted sessions
    "Sim",
    "Match",
    "ExpectTimeout",
    "EngineNotFound",
    "NotSupported",
    "rust_engine_dir",
    # shared
    "BinaryNotFound",
]
