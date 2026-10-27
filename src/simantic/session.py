"""Drive a live simulation from Python, step by step.

`Sim` controls one emulation hosted in this process: it advances virtual
time only on request and otherwise observes without perturbing, so a script
replays the same firmware behaviour every run and Python think-time costs
nothing. It is the programmatic face of everything `sim` can do; pytest is
one place to use it, a plain script or a process pool is another.

    from simantic import Sim

    with Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2") as sim:
        sim.expect(">>> ")                 # run until the prompt, then hold
        sim.send("print(6*7)")             # delivered when time next advances
        sim.expect(r"42\\r?\\n>>> ")        # run until answered
        sim.run_for(0.5)                   # advance exactly 500 virtual ms
        assert "Traceback" not in sim.read_uart()

Platforms: `mcu=` names a model, resolved by the engine like `sim <MCU>`
(your credentials; models are not distributed with this package). The engine
is `simantic_rust`, hosted in this process: one machine, and what it does not
do yet raises `NotSupported` rather than silently no-op. Peers on the board
(`parts=`), several machines and media are `run_scenario`'s.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import telemetry
from ._rust import RustBackend


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


class Sim:
    """One live simulation, driven from Python. Use as a context manager."""

    def __init__(
        self,
        *,
        elf: str | os.PathLike[str] | None = None,
        mcu: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        overlay: str | os.PathLike[str] | None = None,
        scenario: dict[str, Any] | None = None,
        machine: str | None = None,
        uart: str = "uart0",
        symbols_elf: str | os.PathLike[str] | None = None,
        trace_symbols: list[str] = (),
        trace_memory: list[str] = (),
        trace_interrupts: bool = False,
        itm: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        engine_dir: str | os.PathLike[str] | None = None,
    ):
        self.machine = machine
        self.uart = uart
        self._cursors = {"uart": 0, "frames": 0, "logs": 0, "interrupts": 0, "symbol_trace": 0,
                         "memory_trace": 0, "itm": 0}
        # pexpect-style stream: text the firmware printed but no expect() has
        # consumed yet, so sequential expects never miss output that arrived
        # in a previous call's overshoot.
        self._pending: list[tuple[float, str]] = []
        self._base = Path(cwd) if cwd else Path.cwd()

        # Argument errors are the caller's and must not depend on an engine
        # being present.
        if scenario is not None:
            if elf is not None or mcu is not None:
                raise ValueError("scenario= is exclusive with elf=/mcu=")
            if not (scenario.get("machines") or {}):
                raise ValueError("scenario needs at least one machine")
        elif elf is None or mcu is None:
            raise ValueError("give elf= and mcu= (or scenario=)")

        if scenario is not None:
            machines = [dict(name=name, **m) for name, m in scenario["machines"].items()]
            for m in machines:
                if "elf" not in m or "mcu" not in m:
                    raise ValueError(f"machine {m['name']!r} needs elf and mcu")
        else:
            machines = [{"name": "machine", "elf": elf, "mcu": mcu, "parts": parts,
                         "overlay": overlay, "symbolsElfPath": symbols_elf}]
        scenario = scenario or {}

        telemetry.record("sdk.session")
        self._b = RustBackend(
            machines, base=self._base, media=scenario.get("media"),
            services=scenario.get("networkServices"), quantum=scenario.get("quantum"),
            trace_symbols=trace_symbols, trace_memory=trace_memory, trace_interrupts=trace_interrupts,
            itm=itm, engine_dir=engine_dir,
        )
        self.machines: list[str] = list(self._b.machines)

    # -- stimulus -----------------------------------------------------------

    def send(self, text: str, line_ending: str = "\r", uart: str | None = None,
             machine: str | None = None) -> None:
        """Type into the UART. While paused (the normal state between calls)
        the bytes are delivered at the start of the next expect/run_for."""
        self.send_bytes((text + line_ending).encode("latin-1"), uart, machine)

    def send_bytes(self, data: bytes, uart: str | None = None, machine: str | None = None) -> None:
        self._b.send(bytes(data), uart or self.uart, machine or self.machine)

    def inject_gpio(self, peripheral: str, pin: int, state: bool, machine: str | None = None) -> None:
        """Drive an external GPIO input line (a button press/release)."""
        self._b.inject_gpio(peripheral, pin, state, machine or self.machine)

    def inject_can(self, peripheral: str, can_id: int, data: bytes, *, extended: bool = False,
                   remote: bool = False, fd: bool = False, brs: bool = False,
                   machine: str | None = None) -> None:
        """Put a CAN frame on the bus as seen by `peripheral`."""
        self._b.inject_can(peripheral, can_id, bytes(data), extended, remote, fd, brs,
                           machine or self.machine)

    def inject_adc(self, peripheral: str, channel: int, volts: float) -> None:
        """Hold an ADC input channel at `volts` (applies from the next sample)."""
        self._b.inject_adc(peripheral, int(channel), float(volts))

    def attach_i2c(self, peripheral: str, address: int, device: Any) -> None:
        """Make a Python object the I2C device at 7-bit `address` on `peripheral`.

        `device` needs `write(data: bytes)` and `read(count: int) -> bytes`,
        and may have `stop()`. An exception it raises comes out of the next
        `run_for()` / `expect()`.
        """
        self._b.attach_i2c(peripheral, int(address), device)

    def attach_spi(self, peripheral: str, device: Any) -> None:
        """Make a Python callable `device(mosi: int) -> int` the SPI device on `peripheral`.

        Called once per byte exchanged; it returns the byte put on MISO. A
        controller holds one device, so this replaces any the platform attached.
        """
        self._b.attach_spi(peripheral, device)

    def inject_radio(self, peripheral: str, frame: bytes, machine: str | None = None) -> None:
        """Deliver a raw radio frame to a radio peripheral."""
        self._b.inject_radio(peripheral, bytes(frame), machine or self.machine)

    # -- time control -------------------------------------------------------

    def run_for(self, virtual_seconds: float) -> float:
        """Advance exactly this much virtual time, then hold. Returns elapsed virtual time."""
        return self._b.run_for(float(virtual_seconds))

    @property
    def time(self) -> float:
        """Elapsed virtual time in seconds."""
        return self._b.time

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

        matched, live_text, at = self._b.expect(pattern, uart or self.uart, machine or self.machine, float(timeout))
        if not matched:
            raise ExpectTimeout(pattern, text + live_text, at)
        live = rx.search(live_text)
        matched_text = live.group(0) if live else live_text
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
        return Match(matched_text, at)

    # -- observation (never advances time) ----------------------------------

    def read_uart(self, from_start: bool = False) -> str:
        """Everything the firmware printed since the last read (or ever)."""
        recs = self.uart_records(from_start)
        self._pending.clear()
        return "".join(r["text"] for r in recs)

    def read_uart_bytes(self, from_start: bool = False) -> bytes:
        """Raw bytes the firmware sent since the last read (or ever), byte-accurate.

        Prefer this over `read_uart()` for binary protocols (e.g. UBX, MAVLink):
        `read_uart()`'s `str` round-trips through an encoding, this does not.
        """
        recs = self.uart_records(from_start)
        self._pending.clear()
        return b"".join(r["bytes"] for r in recs)

    def uart_records(self, from_start: bool = False) -> list[dict]:
        """Timestamped UART records: {t, machine, label, text, bytes}."""
        return self._records("uart", from_start)

    def frames(self, from_start: bool = False) -> list[dict]:
        """Captured bus frames (CAN/SPI/I2C/BLE/Ethernet) since the last call."""
        return self._records("frames", from_start)

    def logs(self, from_start: bool = False) -> list[dict]:
        """Simulator-side logs — unhandled registers, model warnings."""
        return self._records("logs", from_start)

    def interrupts(self, from_start: bool = False) -> list[dict]:
        """Interrupt entry/exit records (needs trace_interrupts=True)."""
        return self._records("interrupts", from_start)

    def symbol_trace(self, from_start: bool = False) -> list[dict]:
        """Hits on trace_symbols= with their argument registers (non-halting)."""
        return self._records("symbol_trace", from_start)

    def memory_trace(self, from_start: bool = False) -> list[dict]:
        """Accesses to trace_memory= ranges (non-halting): {t, machine, watch, kind,
        address, value}. `value` is None for a read."""
        return self._records("memory_trace", from_start)

    def itm(self, from_start: bool = False) -> list[dict]:
        """ITM stimulus-port output (needs itm=True): {t, machine, port, bytes, text}."""
        return self._records("itm", from_start)

    def read_memory(self, address: int | str, count: int = 4, machine: str | None = None) -> bytes:
        """Read bytes from the system bus; `address` is an int or a symbol name."""
        if isinstance(address, str):
            address = self.symbol(address, machine)
        return self._b.read_memory(int(address), int(count), machine or self.machine)

    def read_u32(self, address: int | str, machine: str | None = None) -> int:
        return int.from_bytes(self.read_memory(address, 4, machine), "little")

    def symbol(self, name: str, machine: str | None = None) -> int:
        """Address of an ELF symbol."""
        return self._b.symbol(name, machine or self.machine)

    def threads(self, machine: str | None = None) -> dict | None:
        """RTOS thread snapshot, e.g. {"rtos": "Zephyr", "threads": [{"name", "state",
        "priority", ...}], "truncated": False}; None when no RTOS is recognised."""
        return self._b.threads(machine or self.machine)

    def heap(self, machine: str | None = None) -> dict | None:
        """Heap report, e.g. {"arenaStart", "arenaSizeBytes", "usedBytes", "freeBytes",
        "largestFreeBlockBytes", "fragmentationRatio", ...}; None when not recognised."""
        return self._b.heap(machine or self.machine)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if getattr(self, "_b", None) is not None:
            self._b.close()
            self._b = None

    def __enter__(self) -> "Sim":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _records(self, key: str, from_start: bool) -> list[dict]:
        cursor = 0 if from_start else self._cursors[key]
        out: list[dict] = []
        while True:
            page, cursor, truncated = self._b.records(key, cursor, 2000)
            out.extend(page)
            if not truncated:
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
