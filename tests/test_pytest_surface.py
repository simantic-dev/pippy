"""The pytest surface, exercised against a real engine and real firmware.

These are the tests a user would write. They run on whichever engine
--sim-backend names (`both` runs each twice), so they double as the evidence
that one suite can target both.
"""
import os
import pytest

FIX = os.environ.get(
    "SIMANTIC_FIXTURES",
    "/Users/anoof/dev/simantic/sim-fixtures/tests/stm/nucleo-f401re",
)
MCU, UART = "STM32F401RE", "usart2"

pytestmark = pytest.mark.skipif(
    not os.path.isdir(FIX), reason=f"no fixture tree at {FIX}"
)


def f401(sim, name):
    return sim(elf=f"{FIX}/{name}/nucleo_f401re.elf", mcu=MCU, uart=UART)


def test_expect_reaches_the_end(sim):
    """The idiomatic shape: one crossing per assertion."""
    s = f401(sim, "mips-profile")
    s.expect("BENCH alu", timeout=30)
    s.expect("SUGGEST PerformanceInMips", timeout=30)
    assert s.time > 0


def test_transcript_is_readable_after_the_run(sim):
    s = f401(sim, "mips-profile")
    s.expect("SUGGEST PerformanceInMips", timeout=30)
    text = s.read_uart(from_start=True)
    assert "BENCH udiv" in text and len(text) > 500


def test_memory_and_time_advance(sim):
    s = f401(sim, "mips-profile")
    s.run_for(0.005)
    first = s.time
    s.read_u32(0x20000000)          # RAM is readable
    s.run_for(0.005)
    assert s.time > first


def test_backend_is_the_one_requested(sim, sim_backend):
    s = f401(sim, "mips-profile")
    assert s.backend == sim_backend


def test_unsupported_capability_skips_not_fails(sim):
    """A multi-machine scenario is Renode-only; on rust this must skip."""
    s = sim(scenario={"machines": {
        "a": {"elf": f"{FIX}/mips-profile/nucleo_f401re.elf", "mcu": MCU},
        "b": {"elf": f"{FIX}/mips-profile/nucleo_f401re.elf", "mcu": MCU},
    }}, uart=UART)
    assert len(s.machines) == 2
