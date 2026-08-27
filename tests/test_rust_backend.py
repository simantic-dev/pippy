"""The Rust backend through Sim, against a fake engine module. No Rust build
required: what is tested is the Python half — platform rendering, symbol
lookup, the expect loop, record paging, and that unsupported calls say so."""
import struct
import sys
import types

import pytest

from simantic import NotSupported, Sim, _elf, _replx
from simantic.engine import load_rust


# -- platform rendering -------------------------------------------------------

def test_render_takes_defaults_and_folds_arithmetic():
    text = "cpu: CPU.CortexM\n    PerformanceInMips: {{RCC.AHBFreq_Value:84000000 / 1000000 * 1.25}}\n" \
           "nvic: X\n    systickFrequency: {{RCC.AHBFreq_Value:84000000}}\n    cpuType: \"{{cpu:cortex-m4f}}\""
    out = _replx.render(text)
    assert "PerformanceInMips: 105\n" in out
    assert "systickFrequency: 84000000\n" in out
    assert 'cpuType: "cortex-m4f"' in out


def test_render_refuses_code_in_templates():
    assert _replx.render("x: {{a:abc}}") == "x: abc"
    assert _replx.render("x: {{a:1+2}}") == "x: 3"


# -- ELF symbols --------------------------------------------------------------

def _tiny_elf(symbols: dict[str, tuple[int, int]]) -> bytes:
    """A minimal ELF32 LE with just .strtab and .symtab; symbols = {name: (value, info)}."""
    strtab = b"\0"
    entries = []
    for name, (value, info) in symbols.items():
        idx = len(strtab)
        strtab += name.encode() + b"\0"
        entries.append(struct.pack("<IIIBBH", idx, value, 0, info, 0, 1))
    symtab = b"\0" * 16 + b"".join(entries)
    header_len = 52
    strtab_off = header_len
    symtab_off = strtab_off + len(strtab)
    shoff = symtab_off + len(symtab)
    def sh(type_, off, size, link, entsize):
        return struct.pack("<IIIIIIIIII", 0, type_, 0, 0, off, size, link, 0, 1, entsize)
    sections = sh(0, 0, 0, 0, 0) + sh(3, strtab_off, len(strtab), 0, 0) + sh(2, symtab_off, len(symtab), 1, 16)
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8
    header = ident + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, shoff, 0, 52, 0, 0, 40, 3, 1)
    return header + strtab + symtab + sections


def test_symbols_strip_thumb_bit_on_functions_only():
    elf = _tiny_elf({"main": (0x08000495, 0x12), "counter": (0x20000010, 0x11)})
    syms = _elf.symbols(elf)
    assert syms["main"] == 0x08000494
    assert syms["counter"] == 0x20000010


# -- Sim over a fake engine ---------------------------------------------------

class FakeSession:
    """Prints 'boot\\n' then 'RESULT: PASS\\n' on usart2, 1 ms apart, from t=2 ms."""
    instances = []

    def __init__(self, repl_text, elf):
        self.repl_text, self.elf, self.t = repl_text, elf, 0.0
        self.sent, self.gpio = [], []
        self._script = [(0.002, b"boot\n"), (0.003, b"RESULT: PASS\n")]
        FakeSession.instances.append(self)

    def run_for(self, s):
        self.t += s

    def time(self):
        return self.t

    def warnings(self):
        return ["spi1 served by the quiet fallback"]

    def take_uart(self):
        out, keep = [], []
        for at, data in self._script:
            if at <= self.t:
                out.append((at, "usart2", data))
            else:
                keep.append((at, data))
        self._script = keep
        return out

    def send_uart(self, uart, data):
        self.sent.append((uart, bytes(data)))

    def inject_gpio(self, p, pin, level):
        self.gpio.append((p, pin, level))

    def read_memory(self, addr, n):
        return bytes(range(n))

    # -- observation: bare metal by default (no kernel, no allocator) --------

    def rtos_name(self):
        return None

    def task_enumeration_available(self):
        return False

    def tasks(self):
        return None

    def heap(self):
        return None

    def switches(self):
        return []

    def task_usage(self, start=0.0, end=None):
        return []

    def interrupts(self):
        # (t, core, vector, entry) -- one SysTick entry/exit pair at 1 ms.
        return [(0.001, 0, 15, True), (0.0010005, 0, 15, False)]

    def isr_usage(self, start=0.0, end=None):
        return ([(15, 5e-07, 1, 5e-07, 1)], self.t - 5e-07)


class KernelSession(FakeSession):
    """A FakeSession whose image has a kernel, so the task views are live."""

    def rtos_name(self):
        return "FreeRTOS"

    def task_enumeration_available(self):
        return True

    def tasks(self):
        # (id, name, state, priority, core, base, size, peak)
        return [(0x2000_0100, "LED1", "running", 3, 0, 0x2000_8000, 512, 128),
                (0x2000_0200, "", "ready", 0, None, 0x2000_9000, 256, None)]

    def switches(self):
        return [(0.0005, 0, 0x2000_0100), (0.002, 0, 0x2000_0200)]

    def task_usage(self, start=0.0, end=None):
        return [(0x2000_0100, 0.0015, 1, 0.0015), (0x2000_0200, 0.001, 1, 0.001)]

    def heap(self):
        return ("ESP-IDF heap_caps", 4000, 3500, 8192, 2)


@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    mod = types.ModuleType("simantic_rust")
    mod.Session = FakeSession
    monkeypatch.setitem(sys.modules, "simantic_rust", mod)
    load_rust.cache_clear()
    FakeSession.instances.clear()
    repl = tmp_path / "board.repl"
    repl.write_text("cpu: CPU.CortexM\n    freq: {{F:84000000}}\n")
    elf = tmp_path / "fw.elf"
    elf.write_bytes(_tiny_elf({"main": (0x08000495, 0x12)}))
    yield repl, elf
    load_rust.cache_clear()


def test_expect_records_and_symbols_on_rust(fake_engine):
    repl, elf = fake_engine
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        assert sim.backend == "rust" and sim.machines == ["machine"]
        assert "freq: 84000000" in FakeSession.instances[0].repl_text
        m = sim.expect("boot")
        assert m.virtual_seconds == pytest.approx(0.002, abs=1e-9)
        m = sim.expect(r"RESULT: (PASS|FAIL)")
        assert "PASS" in m and m.virtual_seconds == pytest.approx(0.003, abs=1e-9)
        assert sim.run_for(0.01) == pytest.approx(0.013, abs=1e-9)
        assert sim.symbol("main") == 0x08000494
        assert sim.read_memory("main", 2) == b"\x00\x01"
        assert [r["text"] for r in sim.uart_records(from_start=True)] == ["boot\n", "RESULT: PASS\n"]
        assert sim.logs()[0]["message"].startswith("spi1")
        sim.send("hi", uart="usart2")
        sim.inject_gpio("gpioc", 13, True)
        assert FakeSession.instances[0].sent == [("usart2", b"hi\r")]
        assert FakeSession.instances[0].gpio == [("gpioc", 13, True)]


def test_unsupported_calls_say_so(fake_engine):
    repl, elf = fake_engine
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        with pytest.raises(NotSupported):
            sim.inject_can("can1", 0x123, b"\x01")
        with pytest.raises(NotSupported):
            sim.inject_radio("radio", b"\x01")
    with pytest.raises(NotSupported, match="symbol tracing"):
        Sim(elf=elf, repl=repl, backend="rust", trace_symbols=["main"])
    with pytest.raises(NotSupported, match="one machine"):
        Sim(scenario={"machines": {"a": {"elf": str(elf), "repl": str(repl)},
                                   "b": {"elf": str(elf), "repl": str(repl)}}}, backend="rust")


def test_backend_name_is_validated():
    with pytest.raises(ValueError, match="backend"):
        Sim(elf="fw.elf", repl="a.repl", backend="qemu")


# -- the contracts pyrenode3 lacks (docs/competitors/pyrenode3.md §4.7/§4.8) --

def test_an_engine_older_than_the_api_is_refused(tmp_path):
    from simantic.engine import EngineTooOld, check_engine_version

    check_engine_version(tmp_path / "0.5.4")     # exactly the minimum
    check_engine_version(tmp_path / "0.6.0")
    check_engine_version(tmp_path / "dev-publish")  # unversioned: developer's own
    with pytest.raises(EngineTooOld, match="0.5.3"):
        check_engine_version(tmp_path / "0.5.3")


def test_engine_failures_keep_their_cause(fake_engine, monkeypatch):
    repl, elf = fake_engine

    def boom(repl_text, elf_bytes):
        raise RuntimeError("repl parse error at line 5")

    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", boom)
    with pytest.raises(Exception) as exc:
        Sim(elf=elf, repl=repl, backend="rust")
    assert "repl parse error at line 5" in str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)


# -- the batching contract (docs/competitors/pyrenode3.md §7 rule 3) ----------

class CountingSession(FakeSession):
    """Reports how many objects crossed the boundary, against bytes delivered."""

    def __init__(self, repl_text, elf):
        super().__init__(repl_text, elf)
        self.handed_over = 0
        self._script = [(0.001, b"x" * 10_000)]

    def take_uart(self):
        runs = super().take_uart()
        self.handed_over += len(runs)
        return runs


def test_a_burst_crosses_the_boundary_as_runs_not_per_byte(fake_engine, monkeypatch):
    """10,000 bytes must not cost 10,000 Python objects.

    At the measured 705 ns per .NET->CPython crossing, per-byte traffic is what
    turns a display frame or a flash write into seconds of pure overhead. The
    engine hands back runs; this pins that so a future change cannot quietly
    regress to one object per byte.
    """
    repl, elf = fake_engine
    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", CountingSession)
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        sim.run_for(0.002)
        text = sim.read_uart(from_start=True)
    session = CountingSession.instances[-1]
    assert len(text) == 10_000
    assert session.handed_over <= 4, f"{session.handed_over} objects for 10,000 bytes"


def test_records_are_paged_not_returned_whole(fake_engine):
    """Observation is pulled in bounded pages, so a long run cannot hand the
    caller one unbounded list built object by object."""
    repl, elf = fake_engine
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        page, cursor, truncated = sim._b.records("uart", 0, 1)
        assert len(page) <= 1 and cursor <= 1 and isinstance(truncated, bool)


# -- observation: the #101 visibility layer, reachable from pytest ------------

def test_a_bare_metal_image_reports_no_kernel_rather_than_failing(fake_engine):
    """Absence of a kernel is an answer, not an error: `None`, not an exception.
    A caller has to be able to tell "no RTOS here" from "an RTOS I could not
    read", and only the first is representable as None."""
    repl, elf = fake_engine
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        assert sim.threads() is None
        assert sim.heap() is None
        assert sim._b.switches() == []


def test_interrupts_are_records_with_the_vector_named(fake_engine):
    repl, elf = fake_engine
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        sim.run_for(0.01)
        entry, exit_ = sim.interrupts(from_start=True)
        assert entry["direction"] == "entry" and exit_["direction"] == "exit"
        assert entry["exception"] == 15 and entry["name"] == "SysTick"
        assert entry["machine"] == "machine" and entry["core"] == 0


def test_the_isr_log_is_cumulative_and_is_not_re_added_each_advance(fake_engine):
    """The engine's ISR log is never drained by reading it, unlike the UART
    queue. Re-slicing from a cursor is what keeps a second run_for from
    duplicating every record recorded during the first."""
    repl, elf = fake_engine
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        sim.run_for(0.01)
        sim.run_for(0.01)
        assert len(sim.interrupts(from_start=True)) == 2


def test_tasks_come_through_as_threads(fake_engine, monkeypatch):
    repl, elf = fake_engine
    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", KernelSession)
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        snap = sim.threads()
        assert snap["rtos"] == "FreeRTOS" and snap["truncated"] is False
        led1, unnamed = snap["threads"]
        assert led1["name"] == "LED1" and led1["state"] == "running" and led1["priority"] == 3
        assert led1["stack"] == {"base": 0x2000_8000, "sizeBytes": 512, "peakUsedBytes": 128}
        # A build that compiled names out, and one that did not paint stacks:
        # both are real configurations, so neither is invented.
        assert unnamed["name"] == "" and unnamed["stack"]["peakUsedBytes"] is None


def test_heap_reports_only_what_the_allocator_actually_tells_us(fake_engine, monkeypatch):
    """`largestFreeBlockBytes`/`fragmentationRatio` exist on the Renode
    backend but not here — this view has no free-list walk. They are absent
    rather than guessed."""
    repl, elf = fake_engine
    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", KernelSession)
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        h = sim.heap()
        assert h["allocator"] == "ESP-IDF heap_caps"
        assert h["arenaSizeBytes"] == 8192 and h["freeBytes"] == 4000
        assert h["usedBytes"] == 4192 and h["peakUsedBytes"] == 4692
        assert h["minimumFreeBytes"] == 3500
        assert "largestFreeBlockBytes" not in h


def test_the_peak_is_none_when_the_allocator_keeps_no_low_water_mark(fake_engine, monkeypatch):
    """Zephyr's `sys_heap` is walked structurally: the chunk chain describes the
    heap as it is now and records no history. The peak is refused rather than
    approximated from the current state, and it takes `minimumFreeBytes` with
    it -- both keys stay present so "not tracked" is distinguishable from 0."""
    repl, elf = fake_engine

    class NoHistory(KernelSession):
        def heap(self):
            return ("Zephyr sys_heap", 3820, None, 4180, 1)

    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", NoHistory)
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        h = sim.heap()
        assert h["allocator"] == "Zephyr sys_heap"
        assert h["arenaSizeBytes"] == 4180 and h["freeBytes"] == 3820
        assert h["usedBytes"] == 360 and h["regions"] == 1
        assert h["minimumFreeBytes"] is None and h["peakUsedBytes"] is None


def test_switch_and_usage_windows_are_available(fake_engine, monkeypatch):
    repl, elf = fake_engine
    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", KernelSession)
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        sim.run_for(0.01)
        assert [s["task"] for s in sim._b.switches()] == [0x2000_0100, 0x2000_0200]
        busiest = sim._b.task_usage()[0]
        assert busiest["task"] == 0x2000_0100 and busiest["runs"] == 1
        usage = sim._b.isr_usage()
        assert usage["vectors"][0]["name"] == "SysTick"
        assert usage["threadSeconds"] > 0


def test_a_riscv_vector_is_not_given_an_arm_name():
    """`vector` is `mcause` on RISC-V, where these integers mean something
    else. An empty name is the honest answer; a wrong one costs more."""
    from simantic._rust import _vector_name

    assert _vector_name(15) == "SysTick"
    assert _vector_name(16) == "IRQ0"
    assert _vector_name(7) == ""


class UnenumerableSession(KernelSession):
    """A kernel whose all-threads list the build left out -- Zephyr without
    CONFIG_THREAD_MONITOR. Only the running thread is ever visible."""

    def rtos_name(self):
        return "Zephyr"

    def task_enumeration_available(self):
        return False

    def tasks(self):
        return [(0x2000_0080, "", "running", 15, 0, 0x2000_0c40, 320, None)]


def test_a_partial_thread_list_is_reported_as_truncated(fake_engine, monkeypatch):
    """The failure this guards against is silent, not loud: without
    CONFIG_THREAD_MONITOR the adapter can only see the running thread, and a
    one-entry list marked complete reads as "this firmware has one thread".
    Measured on the irq-timer fixture, three threads had actually run."""
    repl, elf = fake_engine
    monkeypatch.setattr(sys.modules["simantic_rust"], "Session", UnenumerableSession)
    with Sim(elf=elf, repl=repl, uart="usart2", backend="rust") as sim:
        snap = sim.threads()
        assert snap["rtos"] == "Zephyr"
        assert len(snap["threads"]) == 1
        assert snap["truncated"] is True
