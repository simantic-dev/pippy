"""esp_image: ESP32 ROM-boot image packing, ROM pinning and flash merging.

Pure Python and offline by default. Two tests use real Espressif files when
they are already on the machine (an ESP-IDF install), and one reaches the
network only with SIMANTIC_NETWORK_TESTS=1.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import struct
import tarfile
import urllib.error
from pathlib import Path

import pytest

from simantic import _cli, esp_image as ei


def _phdrs(elf: bytes) -> tuple[int, list[tuple[int, int, bytes]]]:
    (entry,) = struct.unpack_from("<I", elf, 24)
    (phoff,) = struct.unpack_from("<I", elf, 28)
    (phnum,) = struct.unpack_from("<H", elf, 44)
    out = []
    for i in range(phnum):
        _, off, vaddr, _, filesz, memsz, flags, _ = struct.unpack_from("<IIIIIIII", elf, phoff + i * 32)
        assert filesz == memsz
        out.append((vaddr, flags, elf[off : off + filesz]))
    return entry, out


def _rom(size: int) -> bytes:
    data = bytearray(hashlib.shake_256(b"rom").digest(size))
    data[0x1C34:0x1C38] = b"\x6f\xa0\xd3\x3e"  # a real jal, like the good C3 dump
    return bytes(data)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMANTIC_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


# --- layouts ---------------------------------------------------------------


def test_c3_image_has_rom_drom_alias_and_flash():
    rom, flash = _rom(0x60000), b"\xe9" + os.urandom(999)
    entry, segs = _phdrs(ei.build_image("esp32c3", flash, rom=rom + b"extra"))
    assert entry == 0x40000000
    assert segs == [
        (0x40000000, ei.PF_R | ei.PF_X, rom),
        (0x3FF00000, ei.PF_R, rom[0x40000:0x60000]),
        (0x30000000, ei.PF_R | ei.PF_W, flash),
    ]


def test_c6_image_has_no_drom_alias():
    rom = _rom(0x50000)
    entry, segs = _phdrs(ei.build_image("ESP32-C6", b"\xe9flash", rom=rom))
    assert entry == 0x40000000
    assert [(v, len(d)) for v, _, d in segs] == [(0x40000000, 0x50000), (0x30000000, 6)]


def test_writes_out_file(tmp_path):
    out = tmp_path / "image.elf"
    elf = ei.build_image("c3", b"\xe9", rom=_rom(0x60000), out=out)
    assert out.read_bytes() == elf


@pytest.mark.parametrize("name", ["esp32c3", "ESP32-C3", "c3", "esp32_c3"])
def test_chip_names(name):
    assert ei.layout(name).chip == "esp32c3"


def test_unknown_chip():
    with pytest.raises(ei.EspImageError, match="unsupported chip"):
        ei.layout("esp32h2")


# --- ROM checks ------------------------------------------------------------


def test_c3_rom_with_zeroed_phy_table_is_refused():
    rom = bytearray(_rom(0x60000))
    rom[0x1C1C:0x1D00] = bytes(0xE4)
    with pytest.raises(ei.EspImageError, match="PHY"):
        ei.build_image("esp32c3", b"\xe9", rom=bytes(rom))


def test_short_rom_is_refused():
    with pytest.raises(ei.EspImageError, match="too small"):
        ei.build_image("esp32c6", b"\xe9", rom=_rom(0x4FFFF))


def test_debug_elf_is_refused_for_c3():
    with pytest.raises(ei.EspImageError, match="RAW"):
        ei.build_image("esp32c3", b"\xe9", rom=b"\x7fELF" + bytes(0x60000))


def test_p4_objcopy_extract_is_refused():
    with pytest.raises(ei.EspImageError, match="objcopy"):
        ei.build_image("esp32p4", b"\xe9", rom=bytes(0x1FC18))


def test_empty_flash_is_refused():
    with pytest.raises(ei.EspImageError, match="empty"):
        ei.build_image("esp32c3", b"", rom=_rom(0x60000))


# --- flash merging ---------------------------------------------------------


def test_merge_flash_fills_gaps_with_erased_bytes():
    image = ei.merge_flash([(0x10, b"app"), (0x0, b"bl")], size=0x20)
    assert image == b"bl" + b"\xff" * 14 + b"app" + b"\xff" * 13


def test_merge_flash_without_size_ends_at_last_part():
    assert ei.merge_flash([(4, b"x")]) == b"\xff" * 4 + b"x"


def test_merge_flash_rejects_overlap_and_small_size():
    with pytest.raises(ei.EspImageError, match="overlaps"):
        ei.merge_flash([(0, b"abcd"), (2, b"x")])
    with pytest.raises(ei.EspImageError, match="smaller"):
        ei.merge_flash([(0, b"abcd")], size=2)


@pytest.mark.parametrize("text,value", [("16MB", 16 << 20), ("4M", 4 << 20), ("512k", 512 << 10), ("0x1000", 0x1000), ("100", 100)])
def test_parse_size(text, value):
    assert ei.parse_size(text) == value


# --- fetching --------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _pin(monkeypatch, chip, source):
    monkeypatch.setitem(ei.LAYOUTS, chip, dataclasses.replace(ei.LAYOUTS[chip], source=source))


def test_fetch_verifies_caches_and_reuses(home, monkeypatch):
    blob = _rom(0x50000)
    _pin(monkeypatch, "esp32c6", ei.RomSource("https://example.invalid/esp32c6-rom.bin", hashlib.sha256(blob).hexdigest()))
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        return _Response(blob)

    monkeypatch.setattr(ei.urllib.request, "urlopen", urlopen)
    first = ei.fetch_rom("esp32c6")
    assert first.read_bytes() == blob and first.is_relative_to(home)
    assert ei.fetch_rom("esp32c6") == first
    assert len(calls) == 1
    assert _phdrs(ei.build_image("esp32c6", b"\xe9"))[1][0][2] == blob


def test_fetch_refuses_checksum_mismatch(home, monkeypatch):
    _pin(monkeypatch, "esp32c6", ei.RomSource("https://example.invalid/rom.bin", "0" * 64))
    monkeypatch.setattr(ei.urllib.request, "urlopen", lambda request, timeout: _Response(b"tampered"))
    with pytest.raises(ei.EspImageError, match="checksum mismatch"):
        ei.fetch_rom("esp32c6")
    assert not (home / "esp-rom").exists() or not any((home / "esp-rom").iterdir())


def test_fetch_replaces_corrupted_cache(home, monkeypatch):
    blob = _rom(0x50000)
    _pin(monkeypatch, "esp32c6", ei.RomSource("https://example.invalid/esp32c6-rom.bin", hashlib.sha256(blob).hexdigest()))
    monkeypatch.setattr(ei.urllib.request, "urlopen", lambda request, timeout: _Response(blob))
    path = ei.fetch_rom("esp32c6")
    path.write_bytes(b"corrupt")
    assert ei.fetch_rom("esp32c6").read_bytes() == blob


def test_fetch_extracts_and_verifies_tar_member(home, monkeypatch):
    member = b"\x7fELF not really"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("esp32p4_rev0_rom.elf")
        info.size = len(member)
        tar.addfile(info, io.BytesIO(member))
    payload = buf.getvalue()
    _pin(
        monkeypatch,
        "esp32p4",
        ei.RomSource(
            "https://example.invalid/roms.tar.gz",
            hashlib.sha256(payload).hexdigest(),
            member="esp32p4_rev0_rom.elf",
            member_sha256=hashlib.sha256(member).hexdigest(),
        ),
    )
    monkeypatch.setattr(ei.urllib.request, "urlopen", lambda request, timeout: _Response(payload))
    assert ei.fetch_rom("esp32p4").read_bytes() == member


def test_fetch_network_error_points_at_rom_option(home, monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(ei.urllib.request, "urlopen", urlopen)
    with pytest.raises(ei.EspImageError, match="--rom"):
        ei.fetch_rom("esp32c3")


# --- CLI -------------------------------------------------------------------


def test_cli_builds_from_parts(tmp_path, capsys):
    (tmp_path / "rom.bin").write_bytes(_rom(0x60000))
    (tmp_path / "bl.bin").write_bytes(b"BOOT")
    (tmp_path / "app.bin").write_bytes(b"APP")
    out = tmp_path / "image.elf"
    rc = _cli.main([
        "esp-image", "--chip", "esp32c3", "--rom", str(tmp_path / "rom.bin"),
        "--part", f"0x0:{tmp_path / 'bl.bin'}", "--part", f"0x10000:{tmp_path / 'app.bin'}",
        "--flash-size", "128k", "-o", str(out),
    ])
    assert rc == 0
    flash = _phdrs(out.read_bytes())[1][-1][2]
    assert len(flash) == 128 << 10 and flash[:4] == b"BOOT" and flash[0x10000 - 1] == 0xFF
    assert flash[0x10000:0x10003] == b"APP"
    assert "sim --elf" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args,message",
    [
        (["--part", "zz"], "OFFSET:PATH"),
        (["--flash", "missing.bin"], "cannot read flash image"),
        (["--flash", "FLASH", "--rom", "missing-rom.bin"], "cannot read ROM"),
        (["--chip", "esp32h2", "--flash", "FLASH"], "unsupported chip"),
    ],
)
def test_cli_reports_errors_without_traceback(tmp_path, capsys, monkeypatch, args, message):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "FLASH").write_bytes(b"\xe9")
    argv = ["esp-image", "--chip", "esp32c3", "-o", "o.elf"] + args
    if "--rom" not in args:
        argv += ["--rom", "missing-rom.bin"] if "--chip" in args else []
    assert _cli.main(argv) == 1
    assert message in capsys.readouterr().err


# --- real Espressif files, when present ------------------------------------

_IDF_P4 = Path.home() / ".espressif/tools/esp-rom-elfs/20241011/esp32p4_rev0_rom.elf"


@pytest.mark.skipif(not _IDF_P4.exists(), reason="needs ESP-IDF's esp-rom-elfs 20241011")
def test_p4_rom_rebuilt_from_idf_elf_matches_known_good():
    rom = ei.rom_image("esp32p4", _IDF_P4)
    # The ROM image that boots ESP-IDF apps on the simulated P4.
    assert hashlib.sha256(rom).hexdigest() == "a75a4a6e2efc5cefb0b67f52e9871ba5f2fc81bab93f46732f80658cb8eb9cf4"


@pytest.mark.skipif(not os.environ.get("SIMANTIC_NETWORK_TESTS"), reason="set SIMANTIC_NETWORK_TESTS=1 to reach Espressif")
@pytest.mark.parametrize("chip", ei.chips())
def test_pinned_rom_still_downloads(home, chip):
    path = ei.fetch_rom(chip)
    assert ei.rom_image(chip, path)
