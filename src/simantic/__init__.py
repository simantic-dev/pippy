"""Python control of the Simantic simulators.

One package covers both engines, because co-simulation puts them together:
`analog-cli` for circuits and `sim` for firmware.

    import simantic

    with simantic.Sim(elf="fw.elf", repl="board.repl") as sim:   # live control
        sim.expect("ready"); sim.run_for(0.5)
    run = simantic.run_firmware("fw.elf", repl="board.repl",
                                expect=["RESULT: PASS"])          # one-shot
    report = simantic.run_tests("hardware/psu")                   # analog

Neither binary is bundled. Point `$SIMANTIC_ANALOG_CLI` and `$SIMANTIC_SIM`
at them, or put them on PATH.

Installing this package also registers a pytest plugin that turns the
manifests a project already keeps — `*.sim.toml` testplans and `test.yaml`
fixture manifests — into individually addressable pytest items.

The MCP-based agent session lives in `simantic.agent` and is not re-exported
here: `Sim` is the Python surface; MCP is an adapter for chat clients.
"""

from ._locate import BinaryNotFound, analog_cli
from .analog import AnalogCliError, plan_path, plan_test_names, run_tests
from .fixtures import (
    Manifest,
    ModelLibraryUnavailable,
    UnsupportedManifest,
    load_manifest,
)
from .mcu import ServerNotConfigured, SimError, SimRun, sim_binary
from .mcu import run as run_firmware
from .pyrite import pyrite_binary
from .pyrite import run as run_pyrite
from .engine import EngineNotFound, engine_dir
from .session import ExpectTimeout, Match, Sim
from .report import (
    Expect,
    Finding,
    Measurement,
    ReportError,
    Summary,
    Test,
    TestReport,
)

__version__ = "0.1.0"

__all__ = [
    # analog
    "AnalogCliError",
    "Expect",
    "Finding",
    "Measurement",
    "ReportError",
    "Summary",
    "Test",
    "TestReport",
    "analog_cli",
    "plan_path",
    "plan_test_names",
    "run_tests",
    # firmware
    "Manifest",
    "ModelLibraryUnavailable",
    "ServerNotConfigured",
    "SimError",
    "SimRun",
    "UnsupportedManifest",
    "load_manifest",
    "pyrite_binary",
    "run_firmware",
    "run_pyrite",
    "sim_binary",
    # scripted sessions (sim --control-stdio)
    "Sim",
    "Match",
    "ExpectTimeout",
    "EngineNotFound",
    "engine_dir",
    # shared
    "BinaryNotFound",
]
