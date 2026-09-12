"""Bootable image ELFs for the ESP32 ROM-boot platforms (C3, C6, P4).

The ESP32 platforms boot the real Espressif mask ROM, which reads the flash
image over SPI, runs the 2nd-stage bootloader and jumps to the app. The single
ELF the simulator loads therefore carries the mask ROM at the reset vector and
the whole flash image at a backing address the SPI flash device reads by
reference. This module packs those into one ELF:

    from simantic import esp_image
    esp_image.build_image("esp32c3", "flash.bin", out="image.elf")

The mask ROM is Espressif's, and it is not shipped in this package. When no
ROM is given it is downloaded once from Espressif's own public repositories,
pinned by commit and SHA-256, and cached under ``~/.simantic/esp-rom``:

* C3 and C6: the raw dumps QEMU for Espressif ships in ``pc-bios/``.
* P4: ``esp32p4_rev0_rom.elf`` from the ``esp-rom-elfs`` release ESP-IDF
  installs. The ROM debug ELF does not hold the mask ROM verbatim, so the
  bytes are rebuilt from its sections (see ``_p4_rom_from_elf``).

Why not the ``esp-rom-elfs`` ELF for C3/C6 too: its ROM ``.data`` initialisers
sit relocated for the debugger, so a flatten reads zeros where the ROM's
``_init`` expects its function tables. A raw dump is required for those two.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import struct
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .install import simantic_home

EM_RISCV = 243
PF_R, PF_W, PF_X = 4, 2, 1

_QEMU = "https://raw.githubusercontent.com/espressif/qemu/febae182e132e4055529be423a818225ebddaa3a/pc-bios/"
_ROM_ELFS = "https://github.com/espressif/esp-rom-elfs/releases/download/20241011/esp-rom-elfs-20241011.tar.gz"


class EspImageError(RuntimeError):
    """The image could not be built, or the ROM could not be fetched or trusted."""


@dataclass(frozen=True)
class RomSource:
    url: str
    sha256: str
    #: File inside a .tar.gz download, and its own checksum.
    member: str | None = None
    member_sha256: str | None = None


@dataclass(frozen=True)
class Layout:
    chip: str
    reset: int
    rom_size: int
    flash_addr: int
    source: RomSource
    #: (vaddr, start, end): a read-only alias of rom[start:end], C3 only.
    drom: tuple[int, int, int] | None = None


LAYOUTS: dict[str, Layout] = {
    "esp32c3": Layout(
        chip="esp32c3",
        reset=0x40000000,
        rom_size=0x60000,
        flash_addr=0x30000000,
        drom=(0x3FF00000, 0x40000, 0x60000),
        source=RomSource(
            _QEMU + "esp32c3-rom.bin",
            "0de1e65020e803bea0d7443dca149d61895e01fca3bb9c82d073234eebd73f99",
        ),
    ),
    "esp32c6": Layout(
        chip="esp32c6",
        reset=0x40000000,
        rom_size=0x50000,
        flash_addr=0x30000000,
        source=RomSource(
            _QEMU + "esp32c6-rom.bin",
            "91db14c2419391308b108bac0b15fe3442a9c411cdd7f3fb088767c9fbc4b713",
        ),
    ),
    # The P4 mask ROM has its own 128 kB window at 0x4fc00000; 0x44000000 is
    # the free hole between the flash-cache window and PSRAM, used only as the
    # flash image's backing store.
    "esp32p4": Layout(
        chip="esp32p4",
        reset=0x4FC00000,
        rom_size=0x20000,
        flash_addr=0x44000000,
        source=RomSource(
            _ROM_ELFS,
            "921f000164a421c7628fbfee55b173384aafaa51883adc65cd27bf9b0af9e9a9",
            member="esp32p4_rev0_rom.elf",
            member_sha256="948f2c7d108d04a9c3a982f9c1c498fb17f2c4a68abc51f87782592034d7ce44",
        ),
    ),
}


def chips() -> list[str]:
    return sorted(LAYOUTS)


def layout(chip: str) -> Layout:
    """Accepts ``esp32c3``, ``ESP32-C3`` or ``c3``."""
    key = re.sub(r"[^a-z0-9]", "", chip.lower())
    if not key.startswith("esp32"):
        key = "esp32" + key
    try:
        return LAYOUTS[key]
    except KeyError:
        raise EspImageError(f"unsupported chip {chip!r}; expected one of {', '.join(chips())}") from None


# --- the ROM ---------------------------------------------------------------


def rom_cache_dir() -> Path:
    return simantic_home() / "esp-rom"


def _read(value: str | os.PathLike | bytes, what: str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return Path(value).read_bytes()
    except OSError as exc:
        raise EspImageError(f"cannot read {what} {value}: {exc.strerror}") from None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_rom(chip: str, *, force: bool = False, timeout: float = 120) -> Path:
    """The pinned Espressif ROM for ``chip``, downloaded once and cached.

    Every byte is checked against the pinned SHA-256 before it is written, and
    again when read back from the cache, so a tampered or truncated file is
    never used.
    """
    lay = layout(chip)
    src = lay.source
    want = src.member_sha256 or src.sha256
    name = src.member or src.url.rsplit("/", 1)[-1]
    cached = rom_cache_dir() / f"{lay.chip}-{want[:16]}-{name}"
    if cached.exists() and not force:
        if _sha256(cached.read_bytes()) == want:
            return cached
        cached.unlink()

    try:
        with urllib.request.urlopen(urllib.request.Request(src.url), timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise EspImageError(
            f"ROM download failed: HTTP {exc.code} from {src.url}. Pass the ROM yourself with rom=/--rom."
        ) from None
    except urllib.error.URLError as exc:
        raise EspImageError(
            f"cannot reach {src.url}: {exc.reason}. Pass the ROM yourself with rom=/--rom."
        ) from None

    got = _sha256(payload)
    if got != src.sha256:
        raise EspImageError(f"ROM download checksum mismatch (expected {src.sha256}, got {got}); refusing to use it")
    if src.member:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
                member = tar.extractfile(src.member)
                if member is None:
                    raise KeyError(src.member)
                payload = member.read()
        except (tarfile.TarError, KeyError) as exc:
            raise EspImageError(f"{src.member} not found in {src.url}: {exc}") from None
        got = _sha256(payload)
        if got != src.member_sha256:
            raise EspImageError(
                f"{src.member} checksum mismatch (expected {src.member_sha256}, got {got}); refusing to use it"
            )

    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_name(cached.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(cached)
    return cached


def rom_image(chip: str, rom: str | os.PathLike | bytes | None = None) -> bytes:
    """The exact bytes the image maps at the reset vector.

    ``rom`` may be a path or bytes: a raw dump for C3/C6; for P4 either the
    ``esp32p4_rev0_rom.elf`` debug ELF or an already rebuilt 0x20000-byte bin.
    ``None`` uses the pinned download.
    """
    lay = layout(chip)
    if rom is None:
        rom = fetch_rom(lay.chip)
    data = _read(rom, "ROM")

    if lay.chip == "esp32p4":
        if data[:4] == b"\x7fELF":
            return _p4_rom_from_elf(data)
        if len(data) == lay.rom_size:
            return data
        raise EspImageError(
            f"P4 ROM must be esp32p4_rev0_rom.elf or the rebuilt {lay.rom_size:#x}-byte bin, got {len(data):#x} bytes. "
            "A plain objcopy extract is not enough: it zero-fills the ROM's .data init bytes and the ROM "
            "later jumps through a NULL table pointer."
        )

    if data[:4] == b"\x7fELF":
        raise EspImageError(
            f"{lay.chip} needs a RAW mask-ROM dump, not an ELF: the esp-rom-elfs debug ELF has its .data "
            "initialisers relocated. Omit the ROM to download the pinned raw dump."
        )
    if len(data) < lay.rom_size:
        raise EspImageError(f"{lay.chip} ROM too small: {len(data):#x} < {lay.rom_size:#x}")
    data = data[: lay.rom_size]
    if lay.chip == "esp32c3" and data[0x1C34:0x1C38] == b"\0\0\0\0":
        # A circulating C3 dump (esp32c3-api1-20210111-dirty) has a zeroed hole
        # in the PHY dispatch table: every radio enable then dies with an
        # illegal instruction at 0x40001c34.
        raise EspImageError(
            "this C3 ROM has a zeroed PHY jump table at 0x40001c34 (the 20210111-dirty dump); "
            "Bluetooth and Wi-Fi would crash on enable. Omit the ROM to download the good one."
        )
    return data


def _sections(elf: bytes) -> list[dict]:
    if elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        raise EspImageError("expected a 32-bit little-endian ELF")
    (shoff,) = struct.unpack_from("<I", elf, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", elf, 0x2E)
    strtab_off = struct.unpack_from("<IIIIIIIIII", elf, shoff + shstrndx * shentsize)[4]
    out = []
    for i in range(shnum):
        name_off, sh_type, _, addr, off, size, *_ = struct.unpack_from("<IIIIIIIIII", elf, shoff + i * shentsize)
        end = elf.index(b"\0", strtab_off + name_off)
        out.append({"name": elf[strtab_off + name_off : end].decode(), "type": sh_type, "addr": addr, "offset": off, "size": size})
    return out


def _symbol(elf: bytes, secs: list[dict], name: str) -> int:
    symtab = next(s for s in secs if s["name"] == ".symtab")
    strtab = next(s for s in secs if s["name"] == ".strtab")
    for i in range(symtab["size"] // 16):
        name_off, value = struct.unpack_from("<II", elf, symtab["offset"] + i * 16)
        if name_off == 0:
            continue
        start = strtab["offset"] + name_off
        if elf[start : elf.index(b"\0", start)].decode() == name:
            return value
    raise EspImageError(f"symbol {name!r} not found in the ROM ELF")


_P4_CODE_SECTIONS = (".fixed.text", ".init.text", ".text", ".rodata", ".rodata.interface")


def _p4_rom_from_elf(elf: bytes) -> bytes:
    """Rebuild the P4 mask-ROM window from ``esp32p4_rev0_rom.elf``.

    1. Lay the loaded code and rodata sections at their addresses in the
       128 kB window (what ``objcopy -O binary --only-section=...`` gives).
    2. Put back the ROM's ``.data`` initialisers. On silicon they sit in the
       gap after ``.text`` and the ROM's startup copies them into RAM using the
       table at ``_data_table_start`` (12-byte dst_start, dst_end, src
       entries). The debug ELF stores those bytes in separate sections at the
       destination address instead, so each entry's section is found by
       (address, size) and its bytes are written back at ``src``.
    3. Copy every PROGBITS section the ELF places inside the window, which
       brings in the interface pointer table at its very top.

    Left out, step 2 makes ``Cache_Invalidate_All`` call through a NULL
    ``rom_cache_internal_table_ptr``; step 3, a NULL ``ets_rom_layout_p``
    aborts heap init.
    """
    lay = LAYOUTS["esp32p4"]
    base, size = lay.reset, lay.rom_size
    secs = _sections(elf)
    rom = bytearray(size)

    def put(addr: int, data: bytes) -> None:
        off = addr - base
        if off < 0 or off + len(data) > size:
            raise EspImageError(f"ROM data at {addr:#x}+{len(data):#x} falls outside the {size:#x}-byte window")
        rom[off : off + len(data)] = data

    for sec in secs:
        if sec["name"] in _P4_CODE_SECTIONS and sec["size"]:
            put(sec["addr"], elf[sec["offset"] : sec["offset"] + sec["size"]])

    table_start = _symbol(elf, secs, "_data_table_start")
    table_end = _symbol(elf, secs, "_bss_start")
    text = next(s for s in secs if s["name"] == ".text")
    table_off = text["offset"] + (table_start - text["addr"])
    for i in range((table_end - table_start) // 12):
        dst_start, dst_end, src = struct.unpack_from("<III", elf, table_off + i * 12)
        length = dst_end - dst_start
        if length == 0:
            continue
        match = [s for s in secs if s["addr"] == dst_start and s["size"] == length]
        if len(match) != 1:
            raise EspImageError(
                f"ROM copy-table entry {i} [{dst_start:#x}..{dst_end:#x}) matched {len(match)} sections, expected 1"
            )
        put(src, elf[match[0]["offset"] : match[0]["offset"] + length])

    for sec in secs:
        if sec["type"] == 1 and sec["size"] and base <= sec["addr"] < base + size:
            put(sec["addr"], elf[sec["offset"] : sec["offset"] + sec["size"]])
    return bytes(rom)


# --- the flash image -------------------------------------------------------


def parse_size(text: str) -> int:
    """``16MB``, ``4M``, ``0x1000000`` or ``16777216``."""
    m = re.fullmatch(r"\s*(0x[0-9a-fA-F]+|\d+)\s*([kKmM][bB]?)?\s*", text)
    if not m:
        raise EspImageError(f"cannot read size {text!r}; use e.g. 16MB or 0x1000000")
    value = int(m.group(1), 0)
    unit = (m.group(2) or "").lower()[:1]
    return value * {"": 1, "k": 1 << 10, "m": 1 << 20}[unit]


def merge_flash(parts: list[tuple[int, bytes]], *, size: int | None = None, fill: int = 0xFF) -> bytes:
    """Lay ``(offset, data)`` parts into one flash image.

    Gaps are 0xFF, the erased state of NOR flash and what ``esptool merge_bin``
    writes, so an unwritten otadata sector reads as empty and the bootloader
    picks the factory/ota_0 app.
    """
    if not parts:
        raise EspImageError("no flash parts given")
    ordered = sorted(parts, key=lambda p: p[0])
    end = 0
    for offset, data in ordered:
        if offset < end:
            raise EspImageError(f"flash part at {offset:#x} overlaps the previous part (which ends at {end:#x})")
        end = offset + len(data)
    if size is not None and size < end:
        raise EspImageError(f"flash size {size:#x} is smaller than the parts ({end:#x})")
    image = bytearray([fill]) * (size if size is not None else end)
    for offset, data in ordered:
        image[offset : offset + len(data)] = data
    return bytes(image)


def _elf32(entry: int, segments: list[tuple[int, int, bytes]]) -> bytes:
    """A minimal ELF32 with one PT_LOAD per (vaddr, flags, data)."""
    ehsize, phentsize = 52, 32
    header = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8 + struct.pack(
        "<HHIIIIIHHHHHH", 2, EM_RISCV, 1, entry, ehsize, 0, 0, ehsize, phentsize, len(segments), 0, 0, 0
    )
    phdrs, body = b"", b""
    offset = ehsize + phentsize * len(segments)
    for vaddr, flags, data in segments:
        phdrs += struct.pack("<IIIIIIII", 1, offset, vaddr, vaddr, len(data), len(data), flags, 1)
        body += data
        offset += len(data)
    return header + phdrs + body


def build_image(
    chip: str,
    flash: str | os.PathLike | bytes,
    *,
    out: str | os.PathLike | None = None,
    rom: str | os.PathLike | bytes | None = None,
) -> bytes:
    """The bootable ELF for ``chip``: mask ROM at the reset vector plus ``flash``.

    ``flash`` is the whole flash image (bootloader, partition table and app at
    their offsets), a path or bytes; ``merge_flash`` builds one from parts.
    Writes ``out`` when given and returns the ELF bytes either way.
    """
    lay = layout(chip)
    rom_bytes = rom_image(lay.chip, rom)
    flash_bytes = _read(flash, "flash image")
    if not flash_bytes:
        raise EspImageError("flash image is empty")

    segments = [(lay.reset, PF_R | PF_X, rom_bytes)]
    if lay.drom:
        vaddr, start, end = lay.drom
        segments.append((vaddr, PF_R, rom_bytes[start:end]))
    segments.append((lay.flash_addr, PF_R | PF_W, flash_bytes))
    elf = _elf32(lay.reset, segments)
    if out is not None:
        Path(out).write_bytes(elf)
    return elf
