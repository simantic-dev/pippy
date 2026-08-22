"""Scriptable firmware sessions: drive a live simulation step by step.

`Sim` starts `sim --control-stdio` and talks to it over newline-delimited
JSON. Between calls the emulation is paused, so Python think-time costs no
virtual time and a script replays the same firmware behaviour every run.
Single machines and multi-machine scenarios (shared clock, CAN/BLE/Ethernet
media, scripted network peers) use the same class.

    from simantic import Sim

    def test_repl():
        with Sim(elf="fw.elf", repl="board.repl", uart="uart0") as sim:
            sim.expect(">>> ")                 # run until the prompt, then hold
            sim.send("print(6*7)")             # delivered when time next advances
            sim.expect(r"42\\r?\\n>>> ")        # run until answered
            sim.run_for(0.5)                   # advance exactly 500 virtual ms
            assert "Traceback" not in sim.read_uart()

    def test_mqtt_over_lte():
        scenario = {
            "machines": {"c6": {"mcu": "ESP32-C6", "elf": "image.elf"}},
            "networkServices": [{"name": "broker", "host": "192.0.2.1", "port": 1883,
                                 "type": "Antmicro.Renode.Peripherals.Network.ScriptedNetworkService",
                                 "args": "mqtt_broker.py"}],
            "quantum": 0.00001,
        }
        with Sim(scenario=scenario, machine="c6", uart="uart0") as sim:
            m = sim.expect("CONNACK verified", timeout=60)
            assert m.virtual_seconds < 5

Platforms: `repl=` is a platform file you supply; `mcu=` names a model, resolved
from the local model library when `$SIMANTIC_MCU_LIB` is set and otherwise
fetched by `sim` itself (needs `sim auth`). Scenario machines accept the same
two keys (`repl` / `mcu`, plus `overlay`).

The wire protocol is owned by `sim` (see its `--control-stdio` help); this
module is the typed face of it and adds nothing the engine does not do.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import telemetry
from .fixtures import MCU_LIB_ENV, platform_path
from .mcu import SimError, sim_binary


class ExpectTimeout(AssertionError):
    """expect() did not match; carries the text collected while waiting."""

    def __init__(self, pattern: str, text: str, virtual_seconds: float):
        super().__init__(
            f"expected /{pattern}/ did not appear by virtual t={virtual_seconds:.6f}s; "
            f"collected: {text!r}"
        )
        self.pattern = pattern
        self.text = text
        self.virtual_seconds = virtual_seconds


class Match:
    """A successful expect: the matched window and the virtual time of the match."""

    def __init__(self, text: str, virtual_seconds: float):
        self.text = text
        self.virtual_seconds = virtual_seconds

    def __contains__(self, needle: str) -> bool:
        return needle in self.text

    def __repr__(self) -> str:
        return f"Match(t={self.virtual_seconds:.6f}, text={self.text!r})"


class Sim:
    """One live simulation, driven from Python. Use as a context manager."""

    def __init__(
        self,
        *,
        elf: str | os.PathLike[str] | None = None,
        repl: str | os.PathLike[str] | None = None,
        mcu: str | None = None,
        overlay: str | os.PathLike[str] | None = None,
        scenario: dict[str, Any] | str | os.PathLike[str] | None = None,
        machine: str | None = None,
        uart: str = "uart0",
        sim_args: list[str] = (),
        binary: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ):
        self.machine = machine
        self.uart = uart
        self._seq = 0
        self._cursors = {"read_uart": 0, "read_frames": 0, "read_logs": 0}
        # pexpect-style stream: text the firmware printed but no expect() has
        # consumed yet, so sequential expects never miss output that arrived
        # in a previous call's overshoot.
        self._pending: list[tuple[float, str]] = []
        self._work = Path(tempfile.mkdtemp(prefix="simantic-session-"))
        self._base_dir = Path(cwd) if cwd else Path.cwd()

        cmd = [str(sim_binary(binary)), "--control-stdio",
               "--output", str(self._work / "uart.txt"), "--ascii", "--only-messages"]
        if scenario is not None:
            if elf is not None or repl is not None or mcu is not None:
                raise ValueError("scenario= is exclusive with elf=/repl=/mcu=")
            cmd += ["--scenario", str(self._scenario_file(scenario))]
        else:
            if elf is None or (repl is None) == (mcu is None):
                raise ValueError("give elf= and exactly one of repl= or mcu= (or scenario=)")
            cmd += ["--elf", str(elf)]
            cmd += self._platform_args(repl, mcu, overlay)
        cmd += list(sim_args)

        telemetry.record("sdk.session")
        self._proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        ready = self._read_reply(None)
        if not ready.get("ready"):
            raise SimError(f"sim did not become ready: {ready}")
        self.machines: list[str] = ready["machines"]
        if self.machine is None and len(self.machines) == 1:
            self.machine = None  # a single machine needs no name on the wire

    # -- platform / scenario preparation -----------------------------------

    def _platform_args(self, repl, mcu, overlay) -> list[str]:
        if repl is not None:
            if overlay is not None:
                raise ValueError("overlay= applies to mcu=, not repl=")
            return ["--repl", str(repl)]
        if os.environ.get(MCU_LIB_ENV):
            return ["--repl", str(platform_path(
                mcu, Path(overlay) if overlay else None, self._work))]
        if overlay is not None:
            raise ValueError(f"overlay= needs a local model library (${MCU_LIB_ENV})")
        return ["--mcu", mcu]

    def _scenario_file(self, scenario) -> Path:
        if not isinstance(scenario, dict):
            return Path(scenario)
        import yaml  # the package's one dependency; imported lazily like fixtures.py

        spec = json.loads(json.dumps(scenario))  # deep copy, plain types only
        for name, m in spec.get("machines", {}).items():
            if "mcu" in m and os.environ.get(MCU_LIB_ENV):
                overlay = m.pop("overlay", None)
                m["repl"] = str(platform_path(
                    m.pop("mcu"), self._base_dir / overlay if overlay else None, self._work))
            for key in ("repl", "elf"):
                if key in m:
                    m[key] = str(self._base_dir / m[key])
        path = self._work / "scenario.yaml"
        path.write_text(yaml.safe_dump(spec, sort_keys=False))
        return path

    # -- stimulus -----------------------------------------------------------

    def send(self, text: str, line_ending: str = "\r", uart: str | None = None,
             machine: str | None = None) -> None:
        """Type into the UART. While paused (the normal state between calls)
        the bytes are delivered at the start of the next expect/run_for."""
        self._call("send", text=text + line_ending, uart=uart or self.uart,
                   machine=machine or self.machine)

    def send_bytes(self, data: bytes, uart: str | None = None, machine: str | None = None) -> None:
        self._call("send", hex=data.hex(), uart=uart or self.uart, machine=machine or self.machine)

    def inject_gpio(self, peripheral: str, pin: int, state: bool, machine: str | None = None) -> None:
        """Drive an external GPIO input line (a button press/release)."""
        self._call("gpio", peripheral=peripheral, pin=pin, state=state,
                   machine=machine or self.machine)

    def inject_can(self, peripheral: str, can_id: int, data: bytes, *, extended: bool = False,
                   remote: bool = False, fd: bool = False, brs: bool = False,
                   machine: str | None = None) -> None:
        """Put a CAN frame on the bus as seen by `peripheral`."""
        self._call("can", peripheral=peripheral, can_id=can_id, hex=data.hex(), extended=extended,
                   remote=remote, fd=fd, brs=brs, machine=machine or self.machine)

    def inject_radio(self, peripheral: str, frame: bytes, machine: str | None = None) -> None:
        """Deliver a raw radio frame to a radio peripheral."""
        self._call("radio", peripheral=peripheral, hex=frame.hex(), machine=machine or self.machine)

    # -- time control -------------------------------------------------------

    def run_for(self, virtual_seconds: float) -> float:
        """Advance exactly this much virtual time, then hold. Returns elapsed virtual time."""
        return self._call("run_for", seconds=virtual_seconds)["t"]

    @property
    def time(self) -> float:
        """Elapsed virtual time in seconds."""
        return self._call("time")["t"]

    def expect(self, pattern: str, timeout: float = 30, uart: str | None = None,
               machine: str | None = None) -> Match:
        """Run until the UART output matches the regex, then hold.

        Output already printed but not consumed by a previous expect() is
        matched first, without advancing time. `timeout` is wall-clock
        seconds of simulation effort, not virtual time. Raises ExpectTimeout
        (an AssertionError) if the pattern never appears.
        """
        rx = re.compile(pattern)
        self._drain_pending()
        text = "".join(s for _, s in self._pending)
        m = rx.search(text)
        if m:
            t = self._time_at_offset(m.end())
            self._consume(m.end())
            return Match(m.group(0), t)

        r = self._call("expect", pattern=pattern, uart=uart or self.uart, timeout=timeout,
                       machine=machine or self.machine)
        if not r["matched"]:
            raise ExpectTimeout(pattern, text + r["text"], r["t"])
        # The engine reports the window it matched in; hand back just the match.
        live = rx.search(r["text"])
        matched_text = live.group(0) if live else r["text"]
        # Consume the stream through the live match and no further, so lines
        # printed in the overshoot stay buffered for the next expect.
        self._drain_pending()
        text = "".join(s for _, s in self._pending)
        m = rx.search(text)
        if m:
            self._consume(m.end())
        else:
            idx = text.rfind(matched_text)
            if idx >= 0:
                self._consume(idx + len(matched_text))
        return Match(matched_text, r["t"])

    # -- observation --------------------------------------------------------

    def read_uart(self, from_start: bool = False) -> str:
        """Everything the firmware printed since the last read (or ever)."""
        recs = self._records("read_uart", from_start)
        self._pending.clear()
        return "".join(r["text"] for r in recs)

    def uart_records(self, from_start: bool = False) -> list[dict]:
        """Timestamped UART records: {t, machine, label, text}."""
        return self._records("read_uart", from_start)

    def frames(self, from_start: bool = False) -> list[dict]:
        """Captured bus frames (CAN/SPI/I2C/BLE/Ethernet) since the last call."""
        return self._records("read_frames", from_start)

    def logs(self, from_start: bool = False) -> list[dict]:
        """Simulator-side logs — unhandled registers, model warnings."""
        return self._records("read_logs", from_start)

    def read_memory(self, address: int | str, count: int = 4, machine: str | None = None) -> bytes:
        """Read bytes from the system bus; `address` is an int or a symbol name."""
        kw = {"symbol": address} if isinstance(address, str) else {"address": hex(address)}
        return bytes.fromhex(self._call("read_memory", count=count, machine=machine or self.machine, **kw)["hex"])

    def read_u32(self, address: int | str, machine: str | None = None) -> int:
        return int.from_bytes(self.read_memory(address, 4, machine), "little")

    def symbol(self, name: str, machine: str | None = None) -> int:
        """Address of an ELF symbol."""
        return self._call("symbol", name=name, machine=machine or self.machine)["address"]

    def threads(self, machine: str | None = None) -> dict | None:
        """RTOS thread snapshot (Zephyr/FreeRTOS) or None when not recognised."""
        return self._call("threads", machine=machine or self.machine)["value"]

    def heap(self, machine: str | None = None) -> dict | None:
        return self._call("heap", machine=machine or self.machine)["value"]

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self._call("stop")
            except SimError:
                pass
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __enter__(self) -> "Sim":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _records(self, op: str, from_start: bool) -> list[dict]:
        cursor = 0 if from_start else self._cursors[op]
        out: list[dict] = []
        while True:
            r = self._call(op, cursor=cursor)
            out.extend(r["records"])
            cursor = r["next"]
            if not r["truncated"]:
                break
        self._cursors[op] = cursor
        return out

    def _drain_pending(self) -> None:
        for rec in self._records("read_uart", False):
            self._pending.append((rec["t"], rec["text"]))

    def _time_at_offset(self, offset: int) -> float:
        seen = 0
        for t, s in self._pending:
            seen += len(s)
            if seen >= offset:
                return t
        return self._pending[-1][0] if self._pending else 0.0

    def _consume(self, offset: int) -> None:
        while offset > 0 and self._pending:
            t, s = self._pending[0]
            if len(s) <= offset:
                offset -= len(s)
                self._pending.pop(0)
            else:
                self._pending[0] = (t, s[offset:])
                offset = 0

    def _call(self, op: str, **args: Any) -> dict:
        self._seq += 1
        msg = {"id": self._seq, "op": op}
        msg.update({k: v for k, v in args.items() if v is not None})
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SimError(f"sim exited before '{op}':\n{self._stderr()}") from None
        reply = self._read_reply(self._seq)
        if not reply.get("ok"):
            raise SimError(f"{op}: {reply.get('error', reply)}")
        return reply

    def _read_reply(self, expect_id: int | None) -> dict:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise SimError(f"sim exited unexpectedly:\n{self._stderr()}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue  # simulator log line, not a reply
            if not isinstance(reply, dict):
                continue
            if expect_id is None and reply.get("ready") is not None:
                return reply
            if reply.get("id") == expect_id:
                return reply

    def _stderr(self) -> str:
        try:
            return (self._proc.stderr.read() if self._proc.stderr else "")[-4000:]
        except (OSError, ValueError):
            return ""
