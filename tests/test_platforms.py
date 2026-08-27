"""list_platforms / read_platform: pure filesystem, no engine, matching the
MCP server's list_local_platforms / read_platform tools."""

import io
import json

import pytest

from simantic import SimError, list_platforms, list_models, read_platform, elf_symbols
from simantic import auth


def test_list_platforms_finds_repl_and_replx(tmp_path):
    (tmp_path / "stm32f401.repl").write_text("cpu: CPU.CortexM\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "nrf52840.replx").write_text("cpu: CPU.CortexM\n")
    (tmp_path / "notes.txt").write_text("ignore me\n")

    found = list_platforms(tmp_path)
    names = {p["name"] for p in found}
    assert names == {"stm32f401", "nrf52840"}
    assert all(p["size_bytes"] > 0 for p in found)


def test_list_platforms_filters_by_name(tmp_path):
    (tmp_path / "stm32f401.repl").write_text("x\n")
    (tmp_path / "nrf52840.repl").write_text("x\n")

    found = list_platforms(tmp_path, filter="stm32")
    assert [p["name"] for p in found] == ["stm32f401"]


def test_list_platforms_missing_directory_raises(tmp_path):
    with pytest.raises(SimError, match="not found"):
        list_platforms(tmp_path / "nope")


def test_read_platform_returns_text(tmp_path):
    p = tmp_path / "board.repl"
    p.write_text("cpu: CPU.CortexM\n")
    assert read_platform(p) == "cpu: CPU.CortexM\n"


def test_read_platform_missing_file_raises(tmp_path):
    with pytest.raises(SimError, match="not found"):
        read_platform(tmp_path / "missing.repl")


def test_elf_symbols_reads_from_path(tmp_path):
    from test_rust_backend import _tiny_elf

    elf = _tiny_elf({"main": (0x08000495, 0x12)})
    p = tmp_path / "fw.elf"
    p.write_bytes(elf)
    assert elf_symbols(p)["main"] == 0x08000494


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_list_models_returns_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    auth.save("smtc_test", "dev@example.com")

    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse({"models": ["STM32F401RE", "ESP32-C3"]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert list_models() == ["STM32F401RE", "ESP32-C3"]
    assert seen["auth"] == "Bearer smtc_test"


def test_list_models_needs_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SimError, match="needs credentials"):
        list_models()
