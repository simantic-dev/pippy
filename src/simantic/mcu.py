"""Driving the `sim` binary — firmware simulation.

`mcu=` names a model, which `sim` resolves for you and which requires
authentication (`sim auth`); models are not distributed with this package.

A single-ELF `run()` reads the UART text `sim` prints. The verdict comes from
substring matching, which is the contract `test.yaml` manifests use (`expect` /
`expect_absent`) and the reason test firmware conventionally prints a
`RESULT: PASS` marker. `run_scenario()` runs a scenario file and reads the
structured files `sim --record` writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ._locate import locate
from . import telemetry

ENV_VAR = "SIMANTIC_SIM"
BINARY = "sim"


class SimError(RuntimeError):
    """The simulator could not run: a bad platform, a missing ELF, a failed start."""


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
    timeout: float = 15,
    expect: Sequence[str] = (),
    expect_absent: Sequence[str] = (),
    ascii_output: bool = True,
    only_messages: bool = True,
    binary: str | os.PathLike[str] | None = None,
) -> SimRun:
    """Run one firmware ELF (`sim <mcu> run --elf`) and check its UART against expectations.

    Defaults mirror what a fixture wants to read: ASCII rather than hex, and
    message text without the `[t] (LABEL)` prefix.
    """
    telemetry.record("sdk.run_firmware")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "uart.txt"
        cmd = [str(sim_binary(binary)), mcu, "run", "--elf", str(elf),
               "--timeout", str(timeout), "--output", str(out_path)]
        if ascii_output:
            cmd.append("--ascii")
        if only_messages:
            cmd.append("--only-messages")
        proc = _run_sim(cmd)
        output = out_path.read_text(errors="replace") if out_path.exists() else ""

    if proc.returncode != 0 and not output:
        raise SimError(f"sim exited {proc.returncode} with no output\n{proc.stderr.strip()}")

    return SimRun(
        output=output,
        exit_code=proc.returncode,
        missing=[t for t in expect if t not in output],
        forbidden=[t for t in expect_absent if t in output],
    )


def _run_sim(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    # No wall-clock kill: `--timeout` ends the run in virtual time, and how long
    # that takes on the host says nothing about the firmware (the pytest plugin
    # judges a manifest's own `wall:` budget).
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


@dataclass
class ScenarioRun:
    """What a one-shot scenario produced, for every machine, in time order."""

    uart_records: list[dict]
    warnings: list[str]
    interrupts: list[dict] = field(default_factory=list)
    #: `sim --frames` lines: each I2C/SPI/UART/CAN/BLE frame, `[machine] [t s] (label) ...`.
    frames: str = ""
    #: What `sim` printed on stderr, including scripted peers' own output.
    stderr: str = ""
    #: What `sim` printed on stdout, including `--trace-*` lines from `args=`.
    stdout: str = ""

    @property
    def output(self) -> str:
        """The UART text as `sim run --scenario --ascii` prints it, one `[machine] ` prefixed line each."""
        return render_uart(self.uart_records, multi=True)


#: Scenario keys that name files, resolved against the scenario file by `sim`.
_MACHINE_PATHS = ("elf", "overlay", "symbolsElfPath")
_PART_PATHS = ("script", "image")


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_scenario(scenario: dict, timeout: float, *, base: str | os.PathLike[str] | None = None,
                 args: Sequence[str] = (), binary: str | os.PathLike[str] | None = None) -> ScenarioRun:
    """Run every machine of `scenario` (the `sim run --scenario` YAML, as a dict)
    to `timeout` virtual seconds. Relative file paths resolve against `base`
    (default: the cwd). Nothing can be sent or read while it runs; what
    happened is read afterwards from the run's `--record` bundle.

    `args` go to `sim run` as given (`["--trace-symbol", "can_send"]`); what
    they print lands in `stdout`.
    """
    base = Path(base) if base else Path.cwd()
    absolute = lambda v: str(base / v)  # noqa: E731  (Path join keeps an absolute `v` as is)
    machines = {}
    for name, m in scenario["machines"].items():
        m = dict(m, **{k: absolute(m[k]) for k in _MACHINE_PATHS if m.get(k)})
        if m.get("parts"):
            m["parts"] = [dict(p, **{k: absolute(p[k]) for k in _PART_PATHS if p.get(k)}) for p in m["parts"]]
        machines[name] = m
    telemetry.record("sdk.run_scenario")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "scenario.yaml").write_text(yaml.safe_dump(dict(scenario, machines=machines)))
        cmd = [str(sim_binary(binary)), "run", "--scenario", str(tmp / "scenario.yaml"),
               "--timeout", str(timeout), "--record", str(tmp / "rec"), "--frames", str(tmp / "frames.txt"), *args]
        # Script paths inside an overlay are relative to `base`, as `sim` opens them from its cwd.
        proc = _run_sim(cmd, cwd=base)
        rec = tmp / "rec"
        if not (rec / "runs.jsonl").exists():
            raise SimError(f"sim exited {proc.returncode} without a record\n{proc.stderr.strip()}")
        records = [{"t": r["t"], "machine": r["machine"], "label": r["label"], "text": r["text"],
                    "bytes": r["text"].encode("latin-1", "replace")} for r in _jsonl(rec / "uart.jsonl")]
        interrupts = [{"t": r["t"], "machine": r["machine"], "direction": "Enter" if r["edge"] == "entry" else "Exit",
                       "exception": r["vector"], "name": r["name"]} for r in _jsonl(rec / "isr.jsonl")]
        warnings = [r["message"] for r in _jsonl(rec / "log.jsonl")]
        frames = (tmp / "frames.txt").read_text(errors="replace") if (tmp / "frames.txt").exists() else ""
    if proc.returncode != 0:
        raise SimError(f"sim exited {proc.returncode}\n{proc.stderr.strip()}")
    return ScenarioRun(records, warnings, interrupts, frames, proc.stderr, proc.stdout)
