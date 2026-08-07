"""The pyrite firmware runner: argument shape and verdict logic."""

import subprocess

import pytest

from simantic import pyrite


@pytest.fixture(autouse=True)
def binary(tmp_path, monkeypatch):
    fake = tmp_path / "pyrite"
    fake.touch()
    monkeypatch.setenv("SIMANTIC_PYRITE", str(fake))
    return fake


def fake_run(monkeypatch, stdout="", returncode=0):
    seen = []

    def capture(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(pyrite.subprocess, "run", capture)
    return seen


def test_board_and_repl_are_mutually_exclusive():
    for kwargs in ({}, {"board": "stm32f401", "repl": "b.repl"}):
        with pytest.raises(ValueError, match="exactly one"):
            pyrite.run("fw.elf", **kwargs)


def test_builds_the_run_subcommand(monkeypatch):
    seen = fake_run(monkeypatch)
    pyrite.run("fw.elf", board="stm32f401", timeout=9)
    assert seen[0][1:] == ["run", "--elf", "fw.elf", "--timeout", "9",
                           "--board", "stm32f401"]


def test_repl_replaces_the_board(monkeypatch):
    seen = fake_run(monkeypatch)
    pyrite.run("fw.elf", repl="board.repl")
    assert "--repl" in seen[0] and "--board" not in seen[0]


def test_expectations_are_matched_against_the_transcript(monkeypatch):
    fake_run(monkeypatch, stdout="Hello World!\nRESULT: PASS\n")
    run = pyrite.run("fw.elf", board="stm32f401", expect=["RESULT: PASS"])
    assert run.passed
    assert run.runner == "pyrite"


def test_a_missing_expectation_fails_and_is_named(monkeypatch):
    fake_run(monkeypatch, stdout="Hello World!\n")
    run = pyrite.run("fw.elf", board="stm32f401", expect=["RESULT: PASS"])
    assert not run.passed
    assert run.missing == ["RESULT: PASS"]
    assert "RESULT: PASS" in run.failure_report()


def test_a_forbidden_string_fails(monkeypatch):
    fake_run(monkeypatch, stdout="RESULT: FAIL\n")
    run = pyrite.run("fw.elf", board="stm32f401", expect_absent=["RESULT: FAIL"])
    assert not run.passed
    assert run.forbidden == ["RESULT: FAIL"]


def test_a_failure_report_names_pyrite_not_sim(monkeypatch):
    """Two runners exist; a report that named the wrong one would misdirect."""
    fake_run(monkeypatch, stdout="boot\n", returncode=3)
    run = pyrite.run("fw.elf", board="stm32f401")
    assert "pyrite exited 3" in run.failure_report()


def test_a_crash_with_no_output_raises(monkeypatch):
    """No transcript means nothing ran, which is an error rather than a verdict."""
    fake_run(monkeypatch, stdout="", returncode=2)
    with pytest.raises(pyrite.SimError, match="pyrite exited 2"):
        pyrite.run("fw.elf", board="stm32f401")
