"""pytest integration for the firmware simulator.

One collector, one idea: a manifest the project already maintains becomes
individually addressable pytest items, rather than one opaque pass/fail for a
whole suite. That buys `-k` filtering, per-test durations, `--junitxml` rows,
and xdist parallelism without any per-project glue.

- `test.yaml`   — one item per fixture (sim)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ._locate import BinaryNotFound
from .fixtures import (
    MCU_LIB_ENV,
    ModelLibraryUnavailable,
    UnsupportedManifest,
    load_manifest,
    platform_for,
)
from .mcu import ServerNotConfigured, SimError, run as run_firmware
from . import telemetry


#: Node ids this plugin collected, so a project's own unit tests are not
#: counted: what they do is not this package's business to measure.
_OURS: set[str] = set()
_COUNTS = {"passed": 0, "failed": 0, "skipped": 0}


def pytest_runtest_logreport(report):
    if report.when == "call" and report.nodeid in _OURS:
        if report.outcome in _COUNTS:
            _COUNTS[report.outcome] += 1


def pytest_terminal_summary(terminalreporter):
    """Report the shape of the session once, after the results are known.

    Session-level rather than per-test: one request per `pytest` invocation
    keeps this off the critical path, where per-test reporting would turn a
    200-test suite into 200 round trips.
    """
    if any(_COUNTS.values()):
        telemetry.report("pytest-session", **_COUNTS)
    # Session end is the only point in a test run where a round trip costs
    # nobody anything; the spool itself is due at most hourly.
    telemetry.flush()


def pytest_collect_file(parent: pytest.Collector, file_path):
    if file_path.name == "test.yaml":
        return FixtureYamlFile.from_parent(parent, path=file_path)
    return None


class SimulationFailure(Exception):
    """A simulation did not meet its declared expectations.

    Carries the runner's own explanation, which is already the most useful
    thing to print: measured values and margins, or the UART transcript.
    """


class _ReportingItem(pytest.Item):
    """Shared failure rendering: show the report, not a Python traceback."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _OURS.add(self.nodeid)

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, SimulationFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)


# --- sim: test.yaml -------------------------------------------------


class FixtureYamlFile(pytest.File):
    def collect(self):
        yield FirmwareItem.from_parent(self, name=self.path.parent.name)


class FirmwareItem(_ReportingItem):
    """One fixture manifest: boot the ELF, check the UART transcript.

    The platform is resolved by model name, which needs `sim auth`. A local
    model library ($SIMANTIC_MCU_LIB) overrides that and resolves without a
    round trip, which is also what a fixture's `overlay` fragment requires,
    since an overlay edits platform text before the simulator sees it.
    """

    def runtest(self) -> None:
        try:
            manifest = load_manifest(self.path)
        except UnsupportedManifest as exc:
            pytest.skip(str(exc))

        local_models = os.environ.get(MCU_LIB_ENV)
        if manifest.overlay and not local_models:
            pytest.skip(
                f"fixture applies an overlay fragment, which needs a local model: "
                f"set ${MCU_LIB_ENV} to a local model library"
            )

        with tempfile.TemporaryDirectory() as tmp:
            try:
                if local_models:
                    target = {"repl": platform_for(manifest, Path(tmp))}
                else:
                    target = {"mcu": manifest.mcu, "use_cached": True}
                result = run_firmware(
                    manifest.elf_path,
                    timeout=manifest.timeout,
                    expect=manifest.expect,
                    expect_absent=manifest.expect_absent,
                    **target,
                )
            except (BinaryNotFound, ModelLibraryUnavailable, ServerNotConfigured) as exc:
                pytest.skip(str(exc))
            except SimError as exc:
                raise SimulationFailure(str(exc)) from None

        if not result.passed:
            raise SimulationFailure(result.failure_report())

    def reportinfo(self):
        return self.path, 0, f"firmware: {self.name}"


# --- fixtures for hand-written tests ---------------------------------------


@pytest.fixture
def firmware():
    """The sim runner, skipping when no binary is installed.

        def test_boot(firmware):
            run = firmware("fw.elf", repl="board.repl", expect=["RESULT: PASS"])
            assert run.passed, run.failure_report()
    """
    from .mcu import sim_binary

    try:
        sim_binary()
    except BinaryNotFound as exc:
        pytest.skip(str(exc))
    return run_firmware
