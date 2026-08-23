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
                out.extend((at, "usart2", b) for b in data)
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
        with pytest.raises(NotSupported, match="core#183"):
            sim.threads()
        with pytest.raises(NotSupported):
            sim.inject_can("can1", 0x123, b"\x01")
    with pytest.raises(NotSupported, match="one machine"):
        Sim(scenario={"machines": {"a": {"elf": str(elf), "repl": str(repl)},
                                   "b": {"elf": str(elf), "repl": str(repl)}}}, backend="rust")


def test_backend_name_is_validated():
    with pytest.raises(ValueError, match="backend"):
        Sim(elf="fw.elf", repl="a.repl", backend="qemu")
