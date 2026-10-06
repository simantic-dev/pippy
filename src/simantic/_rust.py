"""The Rust backend of `Sim`: `simantic_rust.Session` hosted in this process.

Same vocabulary as the Renode backend, one machine at a time. What the Rust
engine does not do yet raises `NotSupported` rather than silently doing
nothing — the gap list is the table in docs/session-api.md.
"""

from __future__ import annotations

import math
import os
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


_NEWER_ENGINE = "on backend='rust' needs a newer Rust engine: run `simantic install engine-rust`"


class NotSupported(SimError):
    """The Rust backend has no implementation of this yet."""


def run_scenario(scenario: dict, timeout: float, *, base: Path, engine_dir=None) -> tuple[list[dict], list[str]]:
    """Run every machine of `scenario` with its media to `timeout` virtual
    seconds, in one call: the runner behind the Rust `sim --scenario`.
    Returns the UART records `Sim.uart_records()` would and the engine's warnings.

    Nothing can be sent or read while it runs; that is what `Sim` is for, and
    on this backend `Sim` drives one machine.
    """
    engine = load_rust(engine_dir)
    if not hasattr(engine, "run_scenario"):
        raise NotSupported(f"a one-shot scenario {_NEWER_ENGINE}")
    if scenario.get("networkServices"):
        raise NotSupported("backend='rust' does not take network services through simantic yet")
    machines, declared = [], set()
    for name, m in scenario["machines"].items():
        if m.get("parts"):
            raise NotSupported("backend='rust' cannot build parts= yet; use backend='renode'")
        overlay = base / m["overlay"] if m.get("overlay") else None
        if overlay:
            declared |= {f"machine '{name}': {entry}" for entry in re.findall(r"^(\w+)\s*:", overlay.read_text(), re.M)}
        machines.append((name, _replx.platform_text(mcu=m["mcu"], overlay=overlay), (base / m["elf"]).read_bytes()))
    media = [(med["type"], [tuple(end.split(".", 1)) for end in med.get("connect") or []])
             for med in scenario.get("media") or []]
    # A net end is `machine.peripheral.pin`.
    nets = [[(m, p, int(pin)) for m, p, pin in (end.rsplit(".", 2) for end in net)] for net in scenario.get("nets") or []]
    # Script paths inside an overlay are relative to `base`, and the engine
    # opens them relative to the working directory.
    cwd = os.getcwd()
    os.chdir(base)
    try:
        uarts, runs, warnings, skipped = engine.run_scenario(
            machines, media, nets, [], max(1, math.ceil(timeout)), scenario.get("quantum"))
    except Exception as exc:
        raise SimError(str(exc)) from exc
    finally:
        os.chdir(cwd)
    lost = [s for s in skipped if s.split(" (")[0] in declared]
    if lost:
        raise NotSupported(f"backend='rust' has no model for {', '.join(lost)}; use backend='renode'")
    records = [{"t": t, "machine": uarts[i][0], "label": uarts[i][1],
                "text": bytes(data).decode("latin-1"), "bytes": bytes(data)} for t, i, data in runs]
    return records, [*warnings, *(f"{s}: not modeled, reads as zero" for s in skipped)]


class RustBackend:
    def __init__(self, machines: list[dict], *, base: Path, media, services, quantum,
                 trace_symbols, trace_memory, trace_interrupts, itm, engine_dir):
        if len(machines) != 1:
            raise NotSupported("backend='rust' runs one machine; multi-machine scenarios need backend='renode'")
        if media or services:
            raise NotSupported("backend='rust' has no media or network services yet")
        m = machines[0]
        if m.get("parts"):
            raise NotSupported("backend='rust' cannot build parts= yet; use backend='renode'")
        self.machines = [m["name"]]
        self._elf = (base / m["elf"]).read_bytes()
        # Symbols come from the side ELF when the image that boots is stripped.
        self._symbols_elf = (base / m["symbolsElfPath"]).read_bytes() if m.get("symbolsElfPath") else self._elf
        overlay = base / m["overlay"] if m.get("overlay") else None
        text = _replx.platform_text(mcu=m["mcu"], overlay=overlay)
        engine = load_rust(engine_dir)
        # Only what was asked for is passed, so an engine that predates an
        # option still runs every script that does not use it.
        options = {}
        if m.get("symbolsElfPath"):
            options["symbols_elf"] = self._symbols_elf
        if itm:
            options["itm"] = True
        try:
            self._s = engine.Session(text, self._elf, **options)
        except TypeError as exc:
            raise NotSupported(f"{'/'.join(options)} {_NEWER_ENGINE}") from exc
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
        self._itm = itm
        self._cortex_m = "CPU.CortexM" in text
        self._names: dict[int, str] | None = None
        self._records: dict[str, list[dict]] = {k: [] for k in (
            "uart", "frames", "logs", "interrupts", "symbol_trace", "memory_trace", "itm")}
        if (trace_symbols or trace_memory) and not hasattr(self._s, "trace_pc"):
            raise NotSupported(f"symbol and memory tracing {_NEWER_ENGINE}")
        self._traced = {self._address(name): name for name in trace_symbols}
        for address in self._traced:
            self._s.trace_pc(address)
        self._watches = []
        for spec in trace_memory:
            # A symbol or 0xADDR, optionally ":len" in bytes.
            # ponytail: len defaults to 4, not the symbol's size; read st_size in _elf if that bites.
            where, _, length = spec.partition(":")
            address, length = self._address(where), int(length, 0) if length else 4
            self._s.trace_memory(address, length)
            self._watches.append((address, address + length, spec))
        self._records["logs"] = [{"t": 0.0, "level": "Warning", "source": "platform", "message": w}
                                 for w in [*self._s.warnings(), *(f"{e}: not modeled, reads as zero" for e in skipped)]]

    # -- stimulus ---------------------------------------------------------

    def send(self, data: bytes, uart: str, machine: str | None) -> None:
        self._s.send_uart(uart, bytes(data))

    def inject_gpio(self, peripheral: str, pin: int, state: bool, machine: str | None) -> None:
        self._s.inject_gpio(peripheral, int(pin), bool(state))

    def inject_can(self, *_a, **_k) -> None:
        raise NotSupported("backend='rust' has no CAN injection yet")

    def inject_radio(self, *_a, **_k) -> None:
        raise NotSupported("backend='rust' has no radio injection yet")

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
        machine = self.machines[0]
        if self._traced:
            registers = ("r0", "r1", "r2", "r3") if self._cortex_m else ("a0", "a1", "a2", "a3")
            self._records["symbol_trace"].extend(
                {"t": t, "machine": machine, "symbol": self._traced[pc], "address": pc,
                 "args": [{"register": r, "value": v, "symbol": self._name(v)} for r, v in zip(registers, args)]}
                for t, _core, pc, args in self._s.take_pc_trace())
        if self._watches:
            self._records["memory_trace"].extend(
                {"t": t, "machine": machine, "kind": "Write" if write else "Read", "address": address, "value": value,
                 "watch": next((spec for lo, hi, spec in self._watches if lo < address + width and address < hi), None)}
                for t, address, width, value, write in self._s.take_memory_trace())
        if self._itm:
            self._records["itm"].extend(
                {"t": t, "machine": machine, "port": port, "bytes": bytes(data), "text": bytes(data).decode("latin-1")}
                for t, port, data in self._s.take_itm())
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

    def _address(self, where: str) -> int:
        return int(where, 0) if where[:2].lower() == "0x" else self.symbol(where, None)

    def _name(self, address: int) -> str | None:
        """The symbol at exactly `address`, when an argument is a pointer to one."""
        if self._names is None:
            self._names = {v: k for k, v in _elf.symbols(self._symbols_elf, absolute=False).items()}
        return self._names.get(address)

    def symbol(self, name: str, machine: str | None) -> int:
        if self._symbols is None:
            self._symbols = _elf.symbols(self._symbols_elf)
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
