"""The Rust backend of `Sim`: `simantic_rust.Session` hosted in this process.

Same vocabulary as the Renode backend, one machine at a time. What the Rust
engine does not do yet raises `NotSupported` rather than silently doing
nothing — the gap list is simantic-core#183.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import _elf, _replx
from .engine import load_rust
from .mcu import SimError

#: Virtual time advanced between UART checks while waiting in expect().
SLICE_SECONDS = 0.001


class NotSupported(SimError):
    """The Rust backend has no implementation of this yet (simantic-core#183)."""


class RustBackend:
    def __init__(self, machines: list[dict], *, base: Path, media, services, quantum,
                 trace_symbols, trace_interrupts, engine_dir):
        if len(machines) != 1:
            raise NotSupported("backend='rust' runs one machine; multi-machine scenarios need backend='renode'")
        if media or services:
            raise NotSupported("backend='rust' has no media or network services yet (simantic-core#183)")
        if trace_symbols or trace_interrupts:
            raise NotSupported("backend='rust' has no symbol/interrupt tracing yet (simantic-core#183)")
        m = machines[0]
        self.machines = [m["name"]]
        self._elf = (base / m["elf"]).read_bytes()
        repl = base / m["repl"] if m.get("repl") else None
        overlay = base / m["overlay"] if m.get("overlay") else None
        text = _replx.platform_text(repl=repl, mcu=m.get("mcu"), overlay=overlay)
        engine = load_rust(engine_dir)
        try:
            self._s = engine.Session(text, self._elf)
        except Exception as exc:
            raise SimError(str(exc)) from None
        self._symbols: dict[str, int] | None = None
        self._records: dict[str, list[dict]] = {k: [] for k in ("uart", "frames", "logs", "interrupts", "symbol_trace")}
        self._records["logs"] = [{"t": 0.0, "level": "Warning", "source": "platform", "message": w}
                                 for w in self._s.warnings()]

    # -- stimulus ---------------------------------------------------------

    def send(self, data: bytes, uart: str, machine: str | None) -> None:
        self._s.send_uart(uart, bytes(data))

    def inject_gpio(self, peripheral: str, pin: int, state: bool, machine: str | None) -> None:
        self._s.inject_gpio(peripheral, int(pin), bool(state))

    def inject_can(self, *_a, **_k) -> None:
        raise NotSupported("backend='rust' has no CAN injection yet (simantic-core#183)")

    def inject_radio(self, *_a, **_k) -> None:
        raise NotSupported("backend='rust' has no radio injection yet (simantic-core#183)")

    # -- time -------------------------------------------------------------

    def run_for(self, seconds: float) -> float:
        self._advance(seconds)
        return self.time

    @property
    def time(self) -> float:
        return self._s.time()

    def expect(self, pattern: str, uart: str, machine: str | None, timeout: float) -> tuple[bool, str, float]:
        """Run in slices until `pattern` shows up on `uart`; `timeout` is wall-clock."""
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        text = ""
        while True:
            for rec in self._advance(SLICE_SECONDS):
                if rec["label"] == uart:
                    text += rec["text"]
            if rx.search(text):
                return True, text, self.time
            if time.monotonic() > deadline:
                return False, text, self.time

    def _advance(self, seconds: float) -> list[dict]:
        self._s.run_for(float(seconds))
        fresh: list[dict] = []
        for t, label, byte in self._s.take_uart():
            ch = chr(byte)
            if fresh and fresh[-1]["label"] == label:
                fresh[-1]["text"] += ch
            else:
                fresh.append({"t": t, "machine": self.machines[0], "label": label, "text": ch})
        self._records["uart"].extend(fresh)
        return fresh

    # -- observation ------------------------------------------------------

    def records(self, kind: str, cursor: int, limit: int) -> tuple[list[dict], int, bool]:
        items = self._records[kind]
        page = items[cursor : cursor + limit]
        nxt = cursor + len(page)
        return page, nxt, nxt < len(items)

    def read_memory(self, address: int, count: int, machine: str | None) -> bytes:
        try:
            return bytes(self._s.read_memory(int(address), int(count)))
        except Exception as exc:
            raise SimError(str(exc)) from None

    def symbol(self, name: str, machine: str | None) -> int:
        if self._symbols is None:
            self._symbols = _elf.symbols(self._elf)
        try:
            return self._symbols[name]
        except KeyError:
            raise SimError(f"no symbol {name!r} in the ELF") from None

    def threads(self, machine: str | None):
        raise NotSupported("backend='rust' has no RTOS thread view through Sim yet (simantic-core#183)")

    def heap(self, machine: str | None):
        raise NotSupported("backend='rust' has no heap report through Sim yet (simantic-core#183)")

    def close(self) -> None:
        self._s = None
