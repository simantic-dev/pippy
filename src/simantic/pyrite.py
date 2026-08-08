"""Driving the `pyrite` binary — offline Cortex-M firmware runs.

A single self-contained binary: it loads an ELF, runs it for a budget of
virtual time, and writes the UART transcript to stdout. Nothing else has to
be installed or started.

The platform is either a bundled board (`board=`) or an mcu-lib `.repl` file
you supply (`repl=`). Like the other runner, the verdict is substring
matching over the transcript, because UART text is the only observable.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ._locate import locate
from .mcu import SimError, SimRun
from . import telemetry

ENV_VAR = "SIMANTIC_PYRITE"
BINARY = "pyrite"


def pyrite_binary(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the pyrite binary, or raise BinaryNotFound."""
    return locate(BINARY, ENV_VAR, explicit)


def run(
    elf: str | os.PathLike[str],
    *,
    board: str | None = None,
    repl: str | os.PathLike[str] | None = None,
    timeout: int = 5,
    expect: Sequence[str] = (),
    expect_absent: Sequence[str] = (),
    binary: str | os.PathLike[str] | None = None,
) -> SimRun:
    """Run one firmware ELF and check its UART against expectations.

    Give exactly one of `board` (bundled) or `repl` (a platform file).
    `timeout` is a budget of simulated time, not wall-clock.
    """
    if (board is None) == (repl is None):
        raise ValueError("give exactly one of board= or repl=")

    telemetry.record("sdk.run_pyrite")
    cmd = [str(pyrite_binary(binary)), "run", "--elf", str(elf), "--timeout", str(timeout)]
    cmd += ["--board", board] if board is not None else ["--repl", str(repl)]

    # Wall-clock room beyond the virtual-time budget before calling it hung.
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout * 4 + 30
        )
    except subprocess.TimeoutExpired as exc:
        raise SimError(f"pyrite did not exit within its wall-clock budget: {exc}") from None

    output = proc.stdout
    if proc.returncode != 0 and not output.strip():
        raise SimError(f"pyrite exited {proc.returncode}\n{proc.stderr.strip()}")

    return SimRun(
        output=output,
        exit_code=proc.returncode,
        missing=[t for t in expect if t not in output],
        forbidden=[t for t in expect_absent if t in output],
        runner="pyrite",
    )
