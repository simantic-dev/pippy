"""Driving the `sim` binary — firmware simulation.

`mcu=` names a model, which `sim` resolves for you and which requires
authentication (`sim auth`); models are not distributed with this package.

`sim` emits no structured report: the only observable is UART text, written
to --output. The verdict therefore comes from substring matching, which is
the contract `test.yaml` manifests use (`expect` / `expect_absent`) and the
reason test firmware conventionally prints a `RESULT: PASS` marker.

Some installations require a separate simulation server. This module does
not assume either way — it reads that from what the binary reports, so the
same code drives both.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ._locate import locate
from . import telemetry

ENV_VAR = "SIMANTIC_SIM"
BINARY = "sim"

#: The engines `run(backend=)` can use, the same names `Sim(backend=)` takes:
#: "renode" is the default, driven through the `sim` binary; "rust" is the
#: Rust engine, run in this process. They do not support the same targets and
#: peripherals.
BACKENDS = ("renode", "rust")


class SimError(RuntimeError):
    """The simulator could not run: a bad platform, a missing ELF, a failed start."""


class ServerNotConfigured(SimError):
    """This installation needs a simulation server and none was given.

    Separate from SimError because it is an unconfigured environment, not a
    simulation result: a test runner should skip on it, the way it skips on
    a missing binary, rather than report a firmware failure.
    """


@dataclass(frozen=True)
class SimRun:
    """One firmware run: the UART transcript plus the verdict against it."""

    output: str
    exit_code: int
    missing: list[str]
    forbidden: list[str]
    #: Which binary produced this, so a failure names the thing that ran.
    runner: str = "sim"

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.missing and not self.forbidden

    def failure_report(self) -> str:
        lines = []
        if self.exit_code != 0:
            lines.append(f"{self.runner} exited {self.exit_code}")
        for text in self.missing:
            lines.append(f"  expected but not found: {text!r}")
        for text in self.forbidden:
            lines.append(f"  present but forbidden: {text!r}")
        transcript = self.output.strip() or "(no UART output)"
        lines.append("--- UART ---")
        lines.append(transcript)
        return "\n".join(lines)


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


def sim_binary(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the sim binary, or raise BinaryNotFound."""
    return locate(BINARY, ENV_VAR, explicit)


def run(
    elf: str | os.PathLike[str],
    *,
    mcu: str,
    server: str | None = None,
    timeout: int = 15,
    expect: Sequence[str] = (),
    expect_absent: Sequence[str] = (),
    backend: str | None = None,
    use_cached: bool = False,
    ascii_output: bool = True,
    only_messages: bool = True,
    binary: str | os.PathLike[str] | None = None,
) -> SimRun:
    """Run one firmware ELF and check its UART against expectations.

    `mcu` names a backend-resolved model (needs auth). `server` defaults to
    $SIM_SERVER_URL, which `sim` itself reads — it is passed explicitly only
    when given here.

    `use_cached` reuses a previously fetched model from ~/.sim_cache, which
    keeps a suite runnable without a round trip per test.

    Defaults mirror what a fixture wants to read: ASCII rather than hex, and
    message text without the `[t] (LABEL)` prefix.
    """
    if backend is not None and backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}, got {backend!r}")
    telemetry.record("sdk.run_firmware")
    if backend == "rust":
        from ._rust import run_scenario

        records = run_scenario({"machines": {"machine": {"mcu": mcu, "elf": str(elf)}}}, timeout).uart_records
        output = render_uart(records, multi=False)
        return SimRun(output=output, exit_code=0, runner="rust engine",
                      missing=[t for t in expect if t not in output],
                      forbidden=[t for t in expect_absent if t in output])
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "uart.txt"
        cmd = [
            str(sim_binary(binary)),
            "--elf", str(elf),
            "--timeout", str(timeout),
            "--output", str(out_path),
            "--mcu", mcu,
        ]
        if server is not None:
            cmd += ["--server", server]
        if use_cached:
            cmd.append("--use-cached")
        if ascii_output:
            cmd.append("--ascii")
        if only_messages:
            cmd.append("--only-messages")

        # Give the subprocess room past the simulated timeout before treating
        # it as hung: --timeout bounds simulated time, not wall-clock.
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout * 4 + 30
            )
        except subprocess.TimeoutExpired as exc:
            raise SimError(f"sim did not exit within its wall-clock budget: {exc}") from None

        output = out_path.read_text(errors="replace") if out_path.exists() else ""

    if proc.returncode != 0 and not output:
        stderr = proc.stderr.strip()
        # Matched against what sim reports rather than pre-checked: whether a
        # server is needed depends on the installation, not on this package.
        if "sim-server" in stderr:
            raise ServerNotConfigured(
                f"{stderr}\nPass server= or set $SIM_SERVER_URL."
            )
        raise SimError(f"sim exited {proc.returncode} with no output\n{stderr}")

    return SimRun(
        output=output,
        exit_code=proc.returncode,
        missing=[t for t in expect if t not in output],
        forbidden=[t for t in expect_absent if t in output],
    )
