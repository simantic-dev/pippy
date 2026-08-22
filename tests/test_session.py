"""Sim over the stdio protocol, against tests/fake/fake_sim.py. No engine required."""
import os
import stat
import sys
from pathlib import Path

import pytest

from simantic import ExpectTimeout, Sim, SimError

FAKE = Path(__file__).parent / "fake" / "fake_sim.py"


@pytest.fixture
def fake_sim(tmp_path):
    launcher = tmp_path / "sim"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


def test_argument_validation(fake_sim):
    with pytest.raises(ValueError):
        Sim(elf="fw.elf", binary=fake_sim)
    with pytest.raises(ValueError):
        Sim(elf="fw.elf", repl="a.repl", mcu="X", binary=fake_sim)
    with pytest.raises(ValueError):
        Sim(scenario={}, elf="fw.elf", binary=fake_sim)


def test_expect_send_run_for(fake_sim):
    with Sim(elf="fw.elf", repl="board.repl", binary=fake_sim) as sim:
        assert sim.machines == ["m1"]
        m = sim.expect("boot")
        assert m.virtual_seconds > 0
        sim.expect("> ")                      # consumed from the pending stream
        sim.send("hello")
        assert sim.run_for(0.5) == pytest.approx(0.5)
        m = sim.expect(r"echo: hello\r")
        assert "hello" in m
        assert sim.time == pytest.approx(0.5)
        assert sim.read_memory(0x20000000) == b"\xef\xbe\xad\xde"
        assert sim.read_u32("counter") == 0xDEADBEEF
        assert sim.symbol("main") == 0x08000494
        assert sim.threads()["rtos"] == "Zephyr"
        assert sim.frames() == []


def test_expect_timeout_is_assertion(fake_sim):
    with Sim(elf="fw.elf", repl="board.repl", binary=fake_sim) as sim:
        with pytest.raises(ExpectTimeout) as exc:
            sim.expect("never printed", timeout=1)
        assert isinstance(exc.value, AssertionError)
        assert "boot" in exc.value.text


def test_unknown_op_is_sim_error(fake_sim):
    with Sim(elf="fw.elf", repl="board.repl", binary=fake_sim) as sim:
        with pytest.raises(SimError, match="unknown op"):
            sim._call("bogus")


def test_scenario_dict_is_written_relative_to_cwd(fake_sim, tmp_path):
    (tmp_path / "fw.elf").write_bytes(b"")
    scenario = {"machines": {"a": {"repl": "a.repl", "elf": "fw.elf"},
                             "b": {"repl": "b.repl", "elf": "fw.elf"}},
                "quantum": 0.00001}
    with Sim(scenario=scenario, machine="a", binary=fake_sim, cwd=tmp_path) as sim:
        assert sim.machines == ["a", "b"]
        written = (sim._work / "scenario.yaml").read_text()
        assert str(tmp_path / "fw.elf") in written
        assert "quantum: 1.0e-05" in written
