"""Sim against the real engine. Skipped unless the engine is installed
($SIMANTIC_SIM / `simantic install`) and a fixture is available via
$SIMANTIC_SESSION_ELF + $SIMANTIC_SESSION_REPL (a single-machine firmware
that prints RESULT: PASS on $SIMANTIC_SESSION_UART)."""
import os

import pytest

from simantic import ExpectTimeout, Sim
from simantic.engine import EngineNotFound, engine_dir

try:
    engine_dir()
    HAVE_ENGINE = True
except EngineNotFound:
    HAVE_ENGINE = False

ELF = os.environ.get("SIMANTIC_SESSION_ELF")
REPL = os.environ.get("SIMANTIC_SESSION_REPL")
UART = os.environ.get("SIMANTIC_SESSION_UART", "uart0")

needs_engine = pytest.mark.skipif(not (HAVE_ENGINE and ELF and REPL), reason="engine + fixture env not set")


def test_argument_validation():
    with pytest.raises(ValueError):
        Sim(elf="fw.elf", repl="a.repl", mcu="X")
    with pytest.raises(ValueError):
        Sim(scenario={}, elf="fw.elf")


@needs_engine
def test_expect_run_for_and_observers():
    with Sim(elf=ELF, repl=REPL, uart=UART) as sim:
        assert sim.machines == ["machine"]
        m = sim.expect(r"RESULT: (PASS|FAIL)", timeout=120)
        assert "PASS" in m and m.virtual_seconds > 0
        t0 = sim.time
        assert sim.run_for(0.01) == pytest.approx(t0 + 0.01, abs=1e-6)
        main = sim.symbol("main")
        assert main and len(sim.read_memory(main, 4)) == 4
        assert sim.read_memory("main") == sim.read_memory(main)
        assert sim.uart_records(from_start=True)
        assert isinstance(sim.logs(), list)
        t = sim.threads()
        assert t is None or ({"rtos", "threads", "truncated"} <= set(t))
        h = sim.heap()
        assert h is None or "arenaSizeBytes" in h


@needs_engine
def test_expect_timeout_is_assertion():
    with Sim(elf=ELF, repl=REPL, uart=UART) as sim:
        with pytest.raises(ExpectTimeout) as exc:
            sim.expect("never printed by anything", timeout=2)
        assert isinstance(exc.value, AssertionError)


@needs_engine
def test_symbols_elf_single_machine():
    """symbols_elf= reaches SessionMachine.SymbolsElfPath (simantic-core#307)
    without upsetting the run. No stripped-image fixture is wired up here, so
    the companion ELF is the image itself — a smoke test of the plumbing
    (loading the same ELF's symbols twice isn't a real symbol-resolution
    check), not a claim that a stripped image + a genuinely separate
    companion ELF resolves symbols (verified manually against
    sim-fixtures/build/zephyr/zephyr.elf stripped with arm-none-eabi-strip)."""
    with Sim(elf=ELF, repl=REPL, uart=UART, symbols_elf=ELF) as sim:
        m = sim.expect(r"RESULT: (PASS|FAIL)", timeout=120)
        assert "PASS" in m


@needs_engine
def test_symbols_elf_path_in_scenario():
    """symbolsElfPath in a scenario machine dict — same key as the CLI's
    scenario YAML (simantic-cli#184), so a scenario dict stays copy-pasteable."""
    scenario = {"machines": {"machine": {"repl": REPL, "elf": ELF, "symbolsElfPath": ELF}}}
    with Sim(scenario=scenario, uart=UART) as sim:
        m = sim.expect(r"RESULT: (PASS|FAIL)", timeout=120)
        assert "PASS" in m
