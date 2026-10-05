"""pytest integration for the firmware simulator.

One collector, one idea: a manifest the project already maintains becomes
individually addressable pytest items, rather than one opaque pass/fail for a
whole suite. That buys `-k` filtering, per-test durations, `--junitxml` rows,
and xdist parallelism without any per-project glue.

- `test.yaml`   — one item per fixture (sim)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ._locate import BinaryNotFound
from .fixtures import ModelLibraryUnavailable, UnsupportedManifest, load_manifest
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


def render_uart(records: list[dict], multi: bool) -> str:
    """UART records as the `sim --ascii --only-messages` lines a manifest's
    `expect` strings are written against: per (machine, label) stream, lines on
    newline, carriage returns dropped, other non-printables as '.', and a
    `[machine] ` prefix when the scenario has more than one machine."""
    streams: dict[tuple[str, str], str] = {}
    for r in records:
        key = (r["machine"], r["label"])
        streams[key] = streams.get(key, "") + r["text"]
    out = []
    for (machine, _label), text in streams.items():
        prefix = f"[{machine}] " if multi else ""
        for line in text.replace("\r", "").split("\n"):
            clean = "".join(c if 0x20 <= ord(c) <= 0x7E or c == "\t" else "." for c in line)
            out.append(prefix + clean)
    return "\n".join(out)


def render_frames(frames: list[dict], multi: bool) -> str:
    """Bus frames in the `sim --frames` line shape, so `expect_frames` strings
    are the same ones a CLI run would be grepped for:
    `[machine] [t s] (label) PROTO Dir summary`."""
    out = []
    for f in frames:
        prefix = f"[{f['machine']}] " if multi else ""
        out.append(f"{prefix}[{f['t']:.6f}s] ({f['label']}) {f['protocol'].upper()} {f['direction']} {f['summary']}")
    return "\n".join(out)


def _run_within(sim, virtual_s: float, wall_s: float, step: float = 0.5) -> bool:
    """Advance `virtual_s` of virtual time in `step` chunks, giving up once
    `wall_s` of host time has passed. Returns False if the budget ran out.
    A chunk boundary costs a few hundred microseconds, so half-second steps
    are free at the budgets involved."""
    import time

    deadline = time.monotonic() + wall_s
    end = sim.time + virtual_s
    while sim.time < end - 1e-9:
        if time.monotonic() >= deadline:
            return False
        sim.run_for(min(step, end - sim.time))
    return True


def _check(name: str, text: str, want: list[str], forbid: list[str]) -> list[str]:
    problems = []
    for s in want:
        if s not in text:
            problems.append(f"expected {name} to contain {s!r}")
    for s in forbid:
        if s in text:
            problems.append(f"{name} must not contain {s!r}")
    return problems


class FirmwareItem(_ReportingItem):
    """One fixture manifest: boot the machine(s) with their peers, run for the
    manifest's virtual seconds, then check the UART text and bus frames.

    Every manifest runs as a scenario through `Sim`, so `mcu:` names resolve
    through your account (or a local model library, $SIMANTIC_MCU_LIB), overlay
    fragments with scripted peers are applied, and `media:` wires peers to the
    firmware's UARTs and CAN controllers.
    """

    def runtest(self) -> None:
        from .session import Sim

        try:
            manifest = load_manifest(self.path)
        except UnsupportedManifest as exc:
            pytest.skip(str(exc))
        missing = [m["elf"] for m in manifest.machines.values()
                   if not (self.path.parent / m["elf"]).exists()]
        if missing and manifest.elf_external:
            pytest.skip(f"third-party ELF not present: {', '.join(missing)}")
        if missing:
            raise SimulationFailure(f"ELF not found: {', '.join(missing)}")

        with tempfile.TemporaryDirectory() as tmp:
            scenario = manifest.scenario(Path(tmp))
            first = next(iter(scenario["machines"]))
            try:
                with Sim(scenario=scenario, machine=first, cwd=str(self.config.rootpath)) as sim:
                    over_budget = not _run_within(sim, manifest.timeout, manifest.wall_budget)
                    uart = render_uart(sim.uart_records(from_start=True), not manifest.single)
                    frames = render_frames(sim.frames(from_start=True), not manifest.single)
                    reached = sim.time
            except (BinaryNotFound, ModelLibraryUnavailable, ServerNotConfigured) as exc:
                pytest.skip(str(exc))
            except SimError as exc:
                raise SimulationFailure(str(exc)) from None

        problems = []
        if over_budget:
            problems.append(
                f"wall-clock budget of {manifest.wall_budget}s exceeded at virtual t={reached:.3f}s "
                f"of {manifest.timeout}s (set `wall:` in test.yaml if this firmware is legitimately slow; "
                f"otherwise it is a model gap or a busy-wait worth finding)")
        problems += _check("UART output", uart, manifest.expect, manifest.expect_absent)
        problems += _check("bus frames", frames, manifest.expect_frames, manifest.expect_frames_absent)
        if problems:
            report = "\n".join(problems) + "\n--- UART ---\n" + (uart or "<empty>")
            if manifest.expect_frames or manifest.expect_frames_absent:
                report += "\n--- frames ---\n" + (frames or "<none>")
            raise SimulationFailure(report)

    def reportinfo(self):
        return self.path, 0, f"firmware: {self.name}"


# --- fixtures for hand-written tests ---------------------------------------


@pytest.fixture
def firmware():
    """The sim runner, skipping when no binary is installed.

        def test_boot(firmware):
            run = firmware("fw.elf", mcu="STM32F401RE", expect=["RESULT: PASS"])
            assert run.passed, run.failure_report()
    """
    from .mcu import sim_binary

    try:
        sim_binary()
    except BinaryNotFound as exc:
        pytest.skip(str(exc))
    return run_firmware


# --- the in-process test surface -------------------------------------------
#
# The fixtures above run a whole manifest through the `sim` binary: one
# subprocess, and with it ~3 s of engine start-up, per fixture. That is the
# right shape for a manifest, which is one coarse pass/fail.
#
# Hand-written tests are the other shape: many assertions against one running
# machine. For those, `sim` below drives the engine *in-process*, so start-up is
# paid once per worker instead of once per test.
#
# One discipline this surface exists to encode (measured; see
# docs/competitors/simantic-py-review-vs-pyrenode3.md §1b): on the Renode
# backend every hand-off between Python and the engine costs ~400-800 us,
# because resuming rendezvouses with Renode's time-source dispatcher threads.
# Reading is free -- it is the pause/resume that is not. So prefer `expect()`,
# which crosses once, over a poll loop that crosses per millisecond. On the
# Rust backend the same hand-off is ~1 us and the discipline does not apply.

BACKEND_OPTION = "--sim-backend"


def pytest_addoption(parser):
    group = parser.getgroup("simantic")
    group.addoption(
        BACKEND_OPTION,
        default="renode",
        choices=["renode", "rust", "both"],
        help="engine the `sim` fixture drives; 'both' runs each test on each.",
    )


def pytest_generate_tests(metafunc):
    """`both` becomes one test item per backend, so failures name the engine."""
    if "sim_backend" not in metafunc.fixturenames:
        return
    choice = metafunc.config.getoption(BACKEND_OPTION)
    backends = ["renode", "rust"] if choice == "both" else [choice]
    metafunc.parametrize("sim_backend", backends, scope="session")


@pytest.fixture(scope="session")
def sim_backend(request):
    """The engine under test. Parametrized by --sim-backend=both."""
    return request.config.getoption(BACKEND_OPTION)


@pytest.fixture(scope="session")
def _sim_engine(sim_backend):
    """Load the engine once per worker, before any test is timed.

    Without this the first test in a process absorbs the whole start-up cost
    and reads as mysteriously slow; with it, start-up is attributed to the
    session where it belongs. Also turns a missing engine into one clear skip
    rather than a failure per test.
    """
    from . import engine

    try:
        engine.load_rust() if sim_backend == "rust" else engine.load()
    except engine.EngineNotFound as exc:
        pytest.skip(f"no {sim_backend} engine: {exc}")
    return sim_backend


@pytest.fixture
def sim(request, sim_backend, _sim_engine):
    """Factory for an in-process simulation, closed when the test ends.

        def test_timer_fires(sim):
            s = sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2")
            s.expect("RESULT: PASS", timeout=8)

    Anything the chosen backend cannot do skips rather than fails, so one suite
    can run on both engines and report honestly what each covers. On failure the
    UART transcript is attached to the report -- what the firmware printed is
    almost always the useful evidence, and it is gone once the session closes.
    """
    from .session import Sim
    from ._rust import NotSupported

    made = []

    def make(**kwargs):
        kwargs.setdefault("backend", sim_backend)
        try:
            s = Sim(**kwargs)
        except NotSupported as exc:
            pytest.skip(str(exc))
        made.append(s)
        return s

    yield make

    failed = getattr(request.node, "_sim_failed", False)
    for s in made:
        if failed:
            try:
                request.node.add_report_section(
                    "call", f"UART ({s.backend})", s.read_uart(from_start=True)
                )
            except Exception:
                pass
        s.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """A capability the chosen backend lacks is a skip, not a failure.

    Scoped to tests that take the `sim` fixture, so this never reinterprets an
    unrelated error. It is what lets one suite run on both engines and report
    what each actually covers instead of a wall of red on the narrower one.
    """
    if "sim" not in getattr(item, "fixturenames", ()):
        yield
        return
    from ._rust import NotSupported

    outcome = yield
    exc = outcome.excinfo
    if exc is not None and issubclass(exc[0], NotSupported):
        outcome.force_exception(pytest.skip.Exception(str(exc[1])))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Let the `sim` fixture's teardown know whether the test failed."""
    report = (yield).get_result()
    if report.when == "call" and report.failed:
        item._sim_failed = True
