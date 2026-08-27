"""Inspect platform description (`.repl`/`.replx`) files without running anything.

Mirrors the MCP server's `list_local_platforms` / `read_platform` /
`validate_platform` tools (see `MCP/src/Simantic.Mcp/Tools/PlatformTools.cs`)
as plain SDK calls, so a script gets the same answers a chat agent would
without going through a tool schema.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ._replx import render
from .mcu import SimError

#: Same default-directory convention as the MCP server.
PLATFORM_DIR_ENV = "SIMANTIC_PLATFORM_DIR"


def list_platforms(directory: str | os.PathLike[str] | None = None,
                    filter: str | None = None) -> list[dict]:
    """`.repl`/`.replx` files under `directory` (default: $SIMANTIC_PLATFORM_DIR).

    Each entry is `{"name", "path", "extension", "size_bytes"}`. `filter` is a
    case-insensitive substring match on the file name, like the MCP tool's.
    """
    search_dir = Path(directory) if directory else Path(os.environ.get(PLATFORM_DIR_ENV, ""))
    if not search_dir or not search_dir.is_dir():
        raise SimError(
            f"platform directory not found: {search_dir or '(none given)'} — pass directory= "
            f"or set ${PLATFORM_DIR_ENV}"
        )
    files = sorted(
        (p for ext in ("*.repl", "*.replx") for p in search_dir.rglob(ext)),
        key=lambda p: p.name,
    )
    if filter:
        needle = filter.lower()
        files = [p for p in files if needle in p.name.lower()]
    return [
        {"name": p.stem, "path": str(p), "extension": p.suffix, "size_bytes": p.stat().st_size}
        for p in files
    ]


def read_platform(path: str | os.PathLike[str]) -> str:
    """The raw text of a `.repl`/`.replx` file."""
    p = Path(path)
    if not p.is_file():
        raise SimError(f"file not found: {p}")
    return p.read_text()


def validate_platform(path: str | os.PathLike[str], *, engine_dir: str | os.PathLike[str] | None = None) -> dict:
    """Parse `path`, render `{{template}}` placeholders, construct every
    peripheral (including scripted Python peripherals) on a throwaway
    machine, and report what would be built — without running any firmware.

    Returns `{"valid": True, "peripherals": [{"name", "type"}, ...]}` or
    `{"valid": False, "error": "..."}`.

    This rebuilds the process-wide engine state (Renode's EmulationManager),
    so it must not run concurrently with a live `Sim(backend="renode")`
    session in this process — the same rule the MCP server enforces between
    `validate_platform` and an open GDB session.
    """
    from .engine import load

    p = Path(path)
    if not p.is_file():
        raise SimError(f"file not found: {p}")

    load(engine_dir)  # ensures Simantic.Core is CLR-referenced
    import clr  # type: ignore[import-not-found]

    clr.AddReference("Simantic.Core")
    import Simantic.Core.Emulation.PlatformValidator as pv  # type: ignore[import-not-found]

    target = p
    tmp: Path | None = None
    if p.suffix == ".replx":
        tmp = Path(tempfile.mkstemp(suffix=".repl")[1])
        tmp.write_text(render(p.read_text()))
        target = tmp
    try:
        result = pv.Validate(str(target))
        if not result.Valid:
            return {"valid": False, "platform": str(p), "error": result.Error}
        return {
            "valid": True,
            "platform": str(p),
            "peripherals": [{"name": per.Name, "type": per.TypeName} for per in result.Peripherals],
        }
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
