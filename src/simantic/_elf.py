"""Symbol addresses from a 32-bit little-endian ELF, with the standard library.

The Renode backend resolves symbols inside the engine. The Rust engine does
not carry a symbol table, so `Sim.symbol()` on that backend reads `.symtab`
here — the same answer, from the same file.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

SHT_SYMTAB = 2
STT_FUNC = 2


def symbols_in_file(path: str | os.PathLike[str]) -> dict[str, int]:
    """Symbol addresses in the ELF at `path` — same table `Sim.symbol()`
    reads on the Rust backend, exposed standalone: no engine, no running
    firmware. Mirrors the MCP server's `elf_symbols` tool."""
    return symbols(Path(path).read_bytes())


def symbols(elf: bytes) -> dict[str, int]:
    # 32-bit LE only (STM32/nRF/ESP32-C class); no 64-bit ELF class here yet
    # (RV64, Cortex-A64) — MCP's elf_symbols uses ELFSharp and has no such
    # limit, so this is a real gap, not a design choice.
    if elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        raise ValueError("only 32-bit little-endian ELF images are supported")
    (shoff,) = struct.unpack_from("<I", elf, 0x20)
    shentsize, shnum = struct.unpack_from("<HH", elf, 0x2E)
    sections = [struct.unpack_from("<IIIIIIIIII", elf, shoff + i * shentsize) for i in range(shnum)]

    out: dict[str, int] = {}
    for sh in sections:
        _, sh_type, _, _, offset, size, link, _, _, entsize = sh
        if sh_type != SHT_SYMTAB or entsize == 0:
            continue
        strtab_off, strtab_size = sections[link][4], sections[link][5]
        strtab = elf[strtab_off : strtab_off + strtab_size]
        for i in range(size // entsize):
            name_idx, value, _, info = struct.unpack_from("<IIIB", elf, offset + i * entsize)
            if name_idx == 0:
                continue
            end = strtab.index(b"\0", name_idx)
            name = strtab[name_idx:end].decode("utf-8", "replace")
            if info & 0xF == STT_FUNC:
                value &= ~1  # Thumb bit is a call-site convention, not the address
            out.setdefault(name, value)
    return out
