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


_CORTEX_M_EXCEPTIONS = {2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault",
                        11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


def _exception_name(vector: int) -> str | None:
    return f"IRQ{vector - 16}" if vector >= 16 else _CORTEX_M_EXCEPTIONS.get(vector)


class NotSupported(SimError):
    """The Rust backend has no implementation of this yet (simantic-core#183)."""


class RustBackend:
    def __init__(self, machines: list[dict], *, base: Path, media, services, quantum,
                 trace_symbols, trace_memory, trace_interrupts, engine_dir):
        if len(machines) != 1:
            raise NotSupported("backend='rust' runs one machine; multi-machine scenarios need backend='renode'")
        if media or services:
            raise NotSupported("backend='rust' has no media or network services yet (simantic-core#183)")
        if trace_symbols or trace_memory:
            raise NotSupported("backend='rust' has no symbol/memory tracing yet (simantic-core#183)")
        m = machines[0]
        if m.get("symbolsElfPath"):
            raise NotSupported("backend='rust' has no symbols_elf support yet (simantic-core#183)")
        if m.get("parts"):
            raise NotSupported("backend='rust' cannot build parts= yet; use backend='renode'")
        self.machines = [m["name"]]
        self._elf = (base / m["elf"]).read_bytes()
        overlay = base / m["overlay"] if m.get("overlay") else None
        text = _replx.platform_text(mcu=m["mcu"], overlay=overlay)
        engine = load_rust(engine_dir)
        try:
            self._s = engine.Session(text, self._elf)
        except Exception as exc:
            raise SimError(str(exc)) from exc
        # An older engine does not report what it left out.
        skipped = getattr(self._s, "skipped", list)()
        declared = set(re.findall(r"^(\w+)\s*:", overlay.read_text(), re.M)) if overlay else set()
        lost = [s for s in skipped if s.split(" ")[0] in declared]
        if lost:
            raise NotSupported(f"backend='rust' has no model for {', '.join(lost)}; use backend='renode'")
        self._symbols: dict[str, int] | None = None
        self._trace_interrupts, self._interrupts_seen = trace_interrupts, 0
        self._cortex_m = "CPU.CortexM" in text
        self._records: dict[str, list[dict]] = {k: [] for k in ("uart", "frames", "logs", "interrupts", "symbol_trace")}
        self._records["logs"] = [{"t": 0.0, "level": "Warning", "source": "platform", "message": w}
                                 for w in [*self._s.warnings(), *(f"{e}: not modeled, reads as zero" for e in skipped)]]

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
        # The engine hands back runs of bytes, not one entry per byte: the
        # per-object boundary cost is what dominates a chatty UART.
        fresh = [{"t": t, "machine": self.machines[0], "label": label,
                  "text": bytes(data).decode("latin-1"), "bytes": bytes(data)}
                 for t, label, data in self._s.take_uart()]
        self._records["uart"].extend(fresh)
        if self._trace_interrupts:
            # ponytail: the engine returns its whole log each call; give the
            # binding a cursor if a long traced run makes this copy show up.
            log = self._s.interrupts()
            self._records["interrupts"].extend(
                {"t": t, "machine": self.machines[0], "direction": "Enter" if entry else "Exit",
                 "exception": vector, "name": _exception_name(vector) if self._cortex_m else None}
                for t, _core, vector, entry in log[self._interrupts_seen:])
            self._interrupts_seen = len(log)
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
            raise SimError(str(exc)) from exc

    def symbol(self, name: str, machine: str | None) -> int:
        if self._symbols is None:
            self._symbols = _elf.symbols(self._elf)
        try:
            return self._symbols[name]
        except KeyError:
            raise SimError(f"no symbol {name!r} in the ELF") from None

    def threads(self, machine: str | None) -> dict | None:
        tasks = self._s.tasks()
        if tasks is None:
            return None
        return {"rtos": self._s.rtos_name(),
                "threads": [{"address": tid, "name": name, "state": state, "priority": priority, "core": core,
                             "stackStart": base, "stackSize": size, "stackHighWaterMarkBytes": peak}
                            for tid, name, state, priority, core, base, size, peak in tasks],
                # Without the kernel's all-threads list only running threads are visible.
                "truncated": not self._s.task_enumeration_available()}

    def heap(self, machine: str | None) -> dict | None:
        report = self._s.heap()
        if report is None:
            return None
        allocator, free, minimum_free, pool, _regions = report
        return {"allocator": allocator, "arenaSizeBytes": pool, "usedBytes": pool - free,
                "freeBytes": free, "minimumFreeBytes": minimum_free}

    def close(self) -> None:
        self._s = None
