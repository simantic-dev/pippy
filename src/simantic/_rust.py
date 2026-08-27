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


def _vector_name(v: int) -> str:
    """Cortex-M exception number to the name a developer recognises.

    Deliberately empty for anything outside the architecturally-defined range
    -- on RISC-V `vector` is `mcause`, where the same integers mean something
    else entirely, and only the platform knows what IRQ 7 is wired to. An
    empty name is the honest answer; a wrong one costs more than none.
    """
    fixed = {2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault",
             11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}
    if v in fixed:
        return fixed[v]
    return f"IRQ{v - 16}" if v >= 16 else ""


class RustBackend:
    def __init__(self, machines: list[dict], *, base: Path, media, services, quantum,
                 trace_symbols, trace_interrupts, engine_dir):
        if len(machines) != 1:
            raise NotSupported("backend='rust' runs one machine; multi-machine scenarios need backend='renode'")
        if media or services:
            raise NotSupported("backend='rust' has no media or network services yet (simantic-core#183)")
        if trace_symbols:
            raise NotSupported("backend='rust' has no symbol tracing yet (simantic-core#183)")
        # trace_interrupts needs no flag here: the engine's exception hook is
        # always on, so interrupts() is served from the log either way. The
        # argument stays accepted so the same test runs on both backends.
        self._trace_interrupts = bool(trace_interrupts)
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
            raise SimError(str(exc)) from exc
        self._symbols: dict[str, int] | None = None
        self._records: dict[str, list[dict]] = {k: [] for k in ("uart", "frames", "logs", "interrupts", "symbol_trace")}
        self._records["logs"] = [{"t": 0.0, "level": "Warning", "source": "platform", "message": w}
                                 for w in self._s.warnings()]
        # How much of the engine's cumulative ISR log has been turned into
        # records already (see _advance).
        self._isr_seen = 0

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
                  "text": bytes(data).decode("latin-1")}
                 for t, label, data in self._s.take_uart()]
        self._records["uart"].extend(fresh)
        # The ISR log is cumulative and never drained by reading, so re-slice
        # from where we left off rather than re-adding what is already there.
        events = self._s.interrupts()
        seen = self._isr_seen
        if len(events) > seen:
            self._records["interrupts"].extend(
                {"t": t, "machine": self.machines[0],
                 "direction": "entry" if entry else "exit",
                 "exception": vector, "name": _vector_name(vector), "core": core}
                for t, core, vector, entry in events[seen:]
            )
            self._isr_seen = len(events)
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

    def threads(self, machine: str | None):
        rtos = self._s.rtos_name()
        if rtos is None:
            return None
        threads = []
        for tid, name, state, priority, core, base, size, peak in self._s.tasks() or ():
            t = {"id": tid, "name": name, "state": state, "priority": priority, "core": core}
            if size is not None:
                # peak is None when the build did not paint stacks; the key is
                # still present so a caller can tell "not painted" from "0
                # used", but it is never invented.
                t["stack"] = {"base": base, "sizeBytes": size, "peakUsedBytes": peak}
            threads.append(t)
        # `truncated` is the adapter's own signal, not a constant. Without
        # the kernel's all-threads list there is no way to see anything but
        # what is currently running, and a one-entry list presented as
        # complete is the worst of the three possible answers.
        #
        # Zephyr needs CONFIG_THREAD_MONITOR for that list to exist (and
        # CONFIG_THREAD_NAME for names, CONFIG_DEBUG_THREAD_INFO for the
        # published offsets, CONFIG_INIT_STACKS for stack high-water). A
        # stock build has none of them, so this is the common case, not the
        # exotic one.
        return {"rtos": rtos, "threads": threads,
                "truncated": not self._s.task_enumeration_available()}

    def heap(self, machine: str | None):
        h = self._s.heap()
        if h is None:
            return None
        allocator, free, minimum_free, pool, regions = h
        # Key names match the Renode backend's where the meaning matches.
        # largestFreeBlockBytes/fragmentationRatio are deliberately absent
        # rather than guessed: this allocator view has no free-list walk, and
        # a fabricated fragmentation number is worse than a missing one.
        #
        # `minimum_free` is None when the allocator keeps no low-water mark to
        # read. ESP-IDF's multi_heap maintains one; Zephyr's sys_heap does not
        # -- its chunk chain describes the heap as it is now and records no
        # history, so a peak is refused rather than approximated from the
        # current state. Both keys stay present and go None together, so a
        # caller can tell "not tracked" from "nothing used".
        peak = None if minimum_free is None else pool - minimum_free
        return {"allocator": allocator, "arenaSizeBytes": pool, "freeBytes": free,
                "usedBytes": pool - free, "minimumFreeBytes": minimum_free,
                "peakUsedBytes": peak, "regions": regions}

    # -- pyrite-only observation ------------------------------------------
    #
    # No Renode counterpart, so these are not on `Sim` -- reach them through
    # `sim._b`. Both are served from logs the engine already fills, so neither
    # halts the machine or perturbs timing.

    def switches(self):
        """Context switches as [{"t", "core", "task"}]; empty without a kernel."""
        return [{"t": t, "core": core, "task": task} for t, core, task in self._s.switches()]

    def task_usage(self, start: float = 0.0, end: float | None = None):
        """Per-task totals over a window: [{"task", "seconds", "runs", "longestRun"}]."""
        return [{"task": tid, "seconds": secs, "runs": runs, "longestRun": longest}
                for tid, secs, runs, longest in self._s.task_usage(start, end)]

    def isr_usage(self, start: float = 0.0, end: float | None = None):
        """Per-vector totals plus thread-mode time, over a window."""
        rows, thread_seconds = self._s.isr_usage(start, end)
        return {"vectors": [{"exception": v, "name": _vector_name(v), "seconds": secs,
                             "count": count, "longest": longest, "maxDepth": depth}
                            for v, secs, count, longest, depth in rows],
                "threadSeconds": thread_seconds}

    def close(self) -> None:
        self._s = None
