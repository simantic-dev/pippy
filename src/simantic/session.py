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
rendered for you); `mcu=` names a model, resolved exactly like `sim --mcu` —
from `~/.sim_cache`, else fetched with your stored credentials and cached —
optionally with an `overlay=` fragment. Scenario machines accept the same
keys. (`$SIMANTIC_MCU_LIB` switches `mcu=` to a local model library for
model development.)

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
from .fixtures import MCU_LIB_ENV, platform_path
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


def _as_dict(net_obj) -> dict | None:
    """An engine record as plain Python (camelCase keys), via the engine's own JSON."""
    if net_obj is None:
        return None
    import json

    import clr  # type: ignore[import-not-found]

    clr.AddReference("System.Text.Json")
    from System.Text.Json import JsonNamingPolicy, JsonSerializer, JsonSerializerOptions  # type: ignore[import-not-found]

    opts = JsonSerializerOptions()
    opts.PropertyNamingPolicy = JsonNamingPolicy.CamelCase
    return json.loads(JsonSerializer.Serialize(net_obj, net_obj.GetType(), opts))


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

        # Argument errors are the caller's and must not depend on an engine
        # being present.
        if scenario is not None:
            if elf is not None or repl is not None or mcu is not None:
                raise ValueError("scenario= is exclusive with elf=/repl=/mcu=")
            if not (scenario.get("machines") or {}):
                raise ValueError("scenario needs at least one machine")
        elif elf is None or (repl is None) == (mcu is None):
            raise ValueError("give elf= and exactly one of repl= or mcu= (or scenario=)")

        ns = load(engine_dir)
        spec = ns.SessionSpec()
        spec.TraceInterrupts = trace_interrupts
        spec.ShowBackendLogs = show_logs
        for s in trace_symbols:
            spec.TraceSymbols.Add(s)

        if scenario is not None:
            self._fill_scenario(spec, scenario)
        else:
            self._add_machine(spec, "machine", repl, mcu, overlay, elf)

        telemetry.record("sdk.session")
        try:
            self._session = ns.Session.Start(spec)
        except Exception as exc:  # .NET exceptions surface as Python exceptions
            raise SimError(f"could not start the simulation: {exc}") from None
        self.machines: list[str] = list(self._session.Machines)

    # -- platform / scenario preparation -----------------------------------

    def _add_machine(self, spec, name: str, repl, mcu, overlay, elf) -> None:
        """Platform file → AddMachine; model name → the local model library when
        $SIMANTIC_MCU_LIB is set (development), else the engine's own resolver
        (~/.sim_cache, then the backend with stored credentials — like `sim --mcu`)."""
        elf_path = str(self._base / elf)
        if repl is not None:
            if overlay is not None:
                raise ValueError("overlay= applies to mcu=, not repl=")
            spec.AddMachine(name, str(self._base / repl), elf_path)
            return
        if os.environ.get(MCU_LIB_ENV):
            platform = platform_path(mcu, self._base / overlay if overlay else None, self._work)
            spec.AddMachine(name, str(platform), elf_path)
            return
        fragment = (self._base / overlay).read_text() if overlay else None
        spec.AddModel(name, mcu, elf_path, fragment)

    def _fill_scenario(self, spec, scenario: dict[str, Any]) -> None:
        machines = scenario.get("machines") or {}
        if not machines:
            raise ValueError("scenario needs at least one machine")
        for name, m in machines.items():
            if "elf" not in m or ("repl" in m) == ("mcu" in m):
                raise ValueError(f"machine {name!r} needs elf and exactly one of repl/mcu")
            self._add_machine(spec, name, m.get("repl"), m.get("mcu"), m.get("overlay"), m["elf"])
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
        """RTOS thread snapshot, e.g. {"rtos": "Zephyr", "threads": [{"name", "state",
        "priority", ...}], "truncated": False}; None when no RTOS is recognised."""
        return _as_dict(self._session.Threads(machine or self.machine))

    def heap(self, machine: str | None = None) -> dict | None:
        """Heap report, e.g. {"arenaStart", "arenaSizeBytes", "usedBytes", "freeBytes",
        "largestFreeBlockBytes", "fragmentationRatio", ...}; None when not recognised."""
        return _as_dict(self._session.Heap(machine or self.machine))

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
