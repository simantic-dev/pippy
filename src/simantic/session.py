"""Drive a live simulation from Python, step by step.

`Sim` controls one emulation hosted in this process: it advances virtual
time only on request and otherwise observes without perturbing, so a script
replays the same firmware behaviour every run and Python think-time costs
nothing. It is the programmatic face of everything `sim` can do; pytest is
one place to use it, a plain script or a process pool is another.

    from simantic import Sim

    with Sim(elf="fw.elf", repl="board.repl", uart="uart0") as sim:
        sim.expect(">>> ")                 # run until the prompt, then hold
        sim.send("print(6*7)")             # delivered when time next advances
        sim.expect(r"42\\r?\\n>>> ")        # run until answered
        sim.run_for(0.5)                   # advance exactly 500 virtual ms
        assert "Traceback" not in sim.read_uart()

    scenario = {
        "machines": {"c6": {"mcu": "ESP32-C6", "elf": "image.elf"}},
        "networkServices": [{"name": "broker", "host": "192.0.2.1", "port": 1883,
                             "type": "Antmicro.Renode.Peripherals.Network.ScriptedNetworkService",
                             "args": "mqtt_broker.py"}],
        "quantum": 0.00001,
    }
    with Sim(scenario=scenario, machine="c6", uart="uart0") as sim:
        assert sim.expect("CONNACK verified", timeout=60).virtual_seconds < 5

Platforms: `repl=` is a platform file you supply (.replx templates are
rendered for you); `mcu=` names a model resolved from the local model library
(`$SIMANTIC_MCU_LIB`), optionally with an `overlay=` fragment. Scenario
machines accept the same keys.

The engine is `Simantic.Core`, hosted in-process (see `engine.py`); this
class adds vocabulary, not semantics.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import telemetry
from .engine import load
from .fixtures import MCU_LIB_ENV, ModelLibraryUnavailable, platform_path
from .mcu import SimError


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
    """A successful expect: the matched text and the virtual time of the match."""

    def __init__(self, text: str, virtual_seconds: float):
        self.text = text
        self.virtual_seconds = virtual_seconds

    def __contains__(self, needle: str) -> bool:
        return needle in self.text

    def __repr__(self) -> str:
        return f"Match(t={self.virtual_seconds:.6f}, text={self.text!r})"


def _bytes(net_bytes) -> bytes:
    return bytes(bytearray(net_bytes)) if net_bytes is not None else b""


def _uart(r) -> dict:
    return {"t": r.T, "machine": r.Machine, "label": r.Label, "text": r.Text}


def _frame(r) -> dict:
    return {"t": r.T, "machine": r.Machine, "label": r.Label, "protocol": r.Protocol,
            "direction": r.Direction, "summary": r.Summary, "id": r.Id,
            "data": _bytes(r.Data) if r.Data is not None else None}


def _log(r) -> dict:
    return {"t": r.T, "level": r.Level, "source": r.Source, "message": r.Message}


def _interrupt(r) -> dict:
    return {"t": r.T, "machine": r.Machine, "direction": r.Direction,
            "exception": int(r.ExceptionIndex), "name": r.Name}


def _symbol_trace(r) -> dict:
    return {"t": r.T, "machine": r.Machine, "symbol": r.Symbol, "address": int(r.Address),
            "args": [{"register": a.Register, "value": int(a.Value), "symbol": a.Symbol} for a in r.Args]}


class Sim:
    """One live simulation, driven from Python. Use as a context manager."""

    def __init__(
        self,
        *,
        elf: str | os.PathLike[str] | None = None,
        repl: str | os.PathLike[str] | None = None,
        mcu: str | None = None,
        overlay: str | os.PathLike[str] | None = None,
        scenario: dict[str, Any] | None = None,
        machine: str | None = None,
        uart: str = "uart0",
        trace_symbols: list[str] = (),
        trace_interrupts: bool = False,
        show_logs: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        engine_dir: str | os.PathLike[str] | None = None,
    ):
        self.machine = machine
        self.uart = uart
        self._cursors = {"uart": 0, "frames": 0, "logs": 0, "interrupts": 0, "symbol_trace": 0}
        # pexpect-style stream: text the firmware printed but no expect() has
        # consumed yet, so sequential expects never miss output that arrived
        # in a previous call's overshoot.
        self._pending: list[tuple[float, str]] = []
        self._work = Path(tempfile.mkdtemp(prefix="simantic-session-"))
        self._base = Path(cwd) if cwd else Path.cwd()

        ns = load(engine_dir)
        spec = ns.SessionSpec()
        spec.TraceInterrupts = trace_interrupts
        spec.ShowBackendLogs = show_logs
        for s in trace_symbols:
            spec.TraceSymbols.Add(s)

        if scenario is not None:
            if elf is not None or repl is not None or mcu is not None:
                raise ValueError("scenario= is exclusive with elf=/repl=/mcu=")
            self._fill_scenario(spec, scenario)
        else:
            if elf is None or (repl is None) == (mcu is None):
                raise ValueError("give elf= and exactly one of repl= or mcu= (or scenario=)")
            platform = self._platform(repl, mcu, overlay)
            spec.AddMachine("machine", str(platform), str(self._base / elf))

        telemetry.record("sdk.session")
        try:
            self._session = ns.Session.Start(spec)
        except Exception as exc:  # .NET exceptions surface as Python exceptions
            raise SimError(f"could not start the simulation: {exc}") from None
        self.machines: list[str] = list(self._session.Machines)

    # -- platform / scenario preparation -----------------------------------

    def _platform(self, repl, mcu, overlay) -> Path:
        if repl is not None:
            if overlay is not None:
                raise ValueError("overlay= applies to mcu=, not repl=")
            return self._base / repl
        if not os.environ.get(MCU_LIB_ENV):
            raise ModelLibraryUnavailable(
                f"mcu= needs a local model library: set ${MCU_LIB_ENV} (or pass repl=)")
        return platform_path(mcu, self._base / overlay if overlay else None, self._work)

    def _fill_scenario(self, spec, scenario: dict[str, Any]) -> None:
        machines = scenario.get("machines") or {}
        if not machines:
            raise ValueError("scenario needs at least one machine")
        for name, m in machines.items():
            if "elf" not in m or ("repl" in m) == ("mcu" in m):
                raise ValueError(f"machine {name!r} needs elf and exactly one of repl/mcu")
            platform = self._platform(m.get("repl"), m.get("mcu"), m.get("overlay"))
            spec.AddMachine(name, str(platform), str(self._base / m["elf"]))
        for med in scenario.get("media") or []:
            sm = spec.AddMedium(med["type"], list(med.get("connect") or []))
            sm.Strict = bool(med.get("strict", False))
            if med.get("hostBridge"):
                sm.HostBridge = med["hostBridge"]
        for svc in scenario.get("networkServices") or []:
            spec.AddService(svc["name"], svc["host"], int(svc.get("port", 0)),
                            svc.get("type", "Antmicro.Renode.Peripherals.Network.EchoService"),
                            self._service_args(svc.get("args", "")))
        if scenario.get("quantum") is not None:
            spec.QuantumSeconds = float(scenario["quantum"])

    def _service_args(self, args: str) -> str:
        # A script path is the common case; make it absolute against cwd=.
        p = self._base / args
        return str(p) if args and p.exists() else args

    # -- stimulus -----------------------------------------------------------

    def send(self, text: str, line_ending: str = "\r", uart: str | None = None,
             machine: str | None = None) -> None:
        """Type into the UART. While paused (the normal state between calls)
        the bytes are delivered at the start of the next expect/run_for."""
        self.send_bytes((text + line_ending).encode("latin-1"), uart, machine)

    def send_bytes(self, data: bytes, uart: str | None = None, machine: str | None = None) -> None:
        self._session.Send(bytes(data), uart or self.uart, machine or self.machine)

    def inject_gpio(self, peripheral: str, pin: int, state: bool, machine: str | None = None) -> None:
        """Drive an external GPIO input line (a button press/release)."""
        self._session.InjectGpio(peripheral, pin, state, machine or self.machine)

    def inject_can(self, peripheral: str, can_id: int, data: bytes, *, extended: bool = False,
                   remote: bool = False, fd: bool = False, brs: bool = False,
                   machine: str | None = None) -> None:
        """Put a CAN frame on the bus as seen by `peripheral`."""
        self._session.InjectCan(peripheral, can_id, bytes(data), extended, remote, fd, brs,
                                machine or self.machine)

    def inject_radio(self, peripheral: str, frame: bytes, machine: str | None = None) -> None:
        """Deliver a raw radio frame to a radio peripheral."""
        self._session.InjectRadio(peripheral, bytes(frame), machine or self.machine)

    # -- time control -------------------------------------------------------

    def run_for(self, virtual_seconds: float) -> float:
        """Advance exactly this much virtual time, then hold. Returns elapsed virtual time."""
        return self._await(self._session.RunForAsync(float(virtual_seconds)))

    @property
    def time(self) -> float:
        """Elapsed virtual time in seconds."""
        return self._session.VirtualTime

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

        r = self._await(self._session.ExpectAsync(pattern, uart or self.uart, machine or self.machine, float(timeout)))
        if not r.Matched:
            raise ExpectTimeout(pattern, text + r.Text, r.VirtualSeconds)
        live = rx.search(r.Text)
        matched_text = live.group(0) if live else r.Text
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
        return Match(matched_text, r.VirtualSeconds)

    # -- observation (never advances time) ----------------------------------

    def read_uart(self, from_start: bool = False) -> str:
        """Everything the firmware printed since the last read (or ever)."""
        recs = self.uart_records(from_start)
        self._pending.clear()
        return "".join(r["text"] for r in recs)

    def uart_records(self, from_start: bool = False) -> list[dict]:
        """Timestamped UART records: {t, machine, label, text}."""
        return self._records("uart", self._session.ReadUart, _uart, from_start)

    def frames(self, from_start: bool = False) -> list[dict]:
        """Captured bus frames (CAN/SPI/I2C/BLE/Ethernet) since the last call."""
        return self._records("frames", self._session.ReadFrames, _frame, from_start)

    def logs(self, from_start: bool = False) -> list[dict]:
        """Simulator-side logs — unhandled registers, model warnings."""
        return self._records("logs", self._session.ReadLogs, _log, from_start)

    def interrupts(self, from_start: bool = False) -> list[dict]:
        """Interrupt entry/exit records (needs trace_interrupts=True)."""
        return self._records("interrupts", self._session.ReadInterrupts, _interrupt, from_start)

    def symbol_trace(self, from_start: bool = False) -> list[dict]:
        """Hits on trace_symbols= with their argument registers (non-halting)."""
        return self._records("symbol_trace", self._session.ReadSymbolTrace, _symbol_trace, from_start)

    def read_memory(self, address: int | str, count: int = 4, machine: str | None = None) -> bytes:
        """Read bytes from the system bus; `address` is an int or a symbol name."""
        if isinstance(address, str):
            address = self.symbol(address, machine)
        return _bytes(self._session.ReadMemory(int(address), int(count), machine or self.machine))

    def read_u32(self, address: int | str, machine: str | None = None) -> int:
        return int.from_bytes(self.read_memory(address, 4, machine), "little")

    def symbol(self, name: str, machine: str | None = None) -> int:
        """Address of an ELF symbol."""
        return int(self._session.ResolveSymbol(name, machine or self.machine))

    def threads(self, machine: str | None = None) -> dict | None:
        """RTOS thread snapshot (Zephyr/FreeRTOS) or None when not recognised."""
        snap = self._session.Threads(machine or self.machine)
        if snap is None:
            return None
        return {"rtos": snap.Rtos, "truncated": snap.Truncated,
                "threads": [{k: getattr(t, k) for k in ("Name", "State", "Priority") if hasattr(t, k)}
                            for t in snap.Threads]}

    def heap(self, machine: str | None = None):
        """Heap report (engine object) or None when not recognised."""
        return self._session.Heap(machine or self.machine)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if getattr(self, "_session", None) is not None:
            self._session.Dispose()
            self._session = None

    def __enter__(self) -> "Sim":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _await(task):
        """Wait for an engine task while releasing the GIL: scripted peers run
        Python on the emulation thread and need it while the clock is running."""
        import time

        while not task.IsCompleted:
            time.sleep(0.0005)
        if task.IsFaulted:
            raise SimError(str(task.Exception.GetBaseException().Message))
        return task.Result

    def _records(self, key: str, reader, convert, from_start: bool) -> list[dict]:
        cursor = 0 if from_start else self._cursors[key]
        out: list[dict] = []
        while True:
            page = reader(cursor, 2000)
            out.extend(convert(r) for r in page.Records)
            cursor = page.Next
            if not page.Truncated:
                break
        self._cursors[key] = cursor
        return out

    def _drain_pending(self) -> None:
        for rec in self.uart_records(False):
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
