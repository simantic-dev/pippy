"""test.yaml fixture manifests: loading, and resolving from a model library.

A fixture names an MCU (`mcu: STM32F401RE`), not a platform file. Normally
`sim` resolves that name for you against your account; models are not
distributed with this package.

If you have a local model library, point $SIMANTIC_MCU_LIB at it and models
resolve from there instead, without a round trip. That path is also what a
fixture's `overlay` fragment needs, since an overlay edits platform text
before it reaches the simulator. Resolution is delegated to the library's own
tooling rather than reimplemented here, so the two cannot drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

#: Points at a local model library. Optional: without it, models resolve
#: through `sim` instead.
MCU_LIB_ENV = "SIMANTIC_MCU_LIB"


class ModelLibraryUnavailable(RuntimeError):
    """No usable model library, so MCU names cannot be resolved locally."""


@dataclass(frozen=True)
class Manifest:
    """A single-machine test.yaml. Multi-machine fixtures are not covered."""

    path: Path
    mcu: str
    elf: str
    timeout: int
    expect: list[str]
    expect_absent: list[str]
    overlay: str | None = None

    @property
    def elf_path(self) -> Path:
        return self.path.parent / self.elf

    @property
    def overlay_path(self) -> Path | None:
        return self.path.parent / self.overlay if self.overlay else None


class UnsupportedManifest(ValueError):
    """The manifest describes a fixture this SDK cannot run yet."""


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    """Parse a test.yaml, or raise UnsupportedManifest with the reason."""
    path = Path(path)
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}

    if "machines" in data:
        raise UnsupportedManifest(
            "multi-machine fixture: needs the sim --scenario runner, which this "
            "SDK does not drive yet"
        )
    if "mcu" not in data:
        raise UnsupportedManifest("manifest names no mcu")

    return Manifest(
        path=path,
        mcu=data["mcu"],
        elf=data["elf"],
        timeout=int(data.get("timeout", 15)),
        expect=list(data.get("expect") or []),
        expect_absent=list(data.get("expect_absent") or []),
        overlay=data.get("overlay"),
    )


def mcu_lib_root() -> Path:
    """The configured model library, or raise ModelLibraryUnavailable."""
    configured = os.environ.get(MCU_LIB_ENV)
    if not configured:
        raise ModelLibraryUnavailable(
            f"set ${MCU_LIB_ENV} to a local model library to resolve MCU models"
        )
    root = Path(configured)
    if not (root / "scripts" / "parse_replx.py").exists():
        raise ModelLibraryUnavailable(
            f"${MCU_LIB_ENV} is {root}, which has no scripts/parse_replx.py "
            "(submodule not initialised?)"
        )
    return root


@cache
def resolved_models() -> Path:
    """Resolve every model once per process; return the output dir.

    parse_replx.py fills `using` directives and strips comments. The result
    is cached for the process because resolving the whole library per test
    would dominate a suite's runtime.
    """
    root = mcu_lib_root()
    dest = Path(tempfile.mkdtemp(prefix="simantic-models-"))
    subprocess.run(
        [sys.executable, str(root / "scripts" / "parse_replx.py"), "models.yaml", str(dest)],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return dest


def platform_for(manifest: Manifest, workdir: Path) -> Path:
    """The .replx to hand `sim --repl`, with any overlay fragment appended."""
    base = resolved_models() / f"{manifest.mcu}.replx"
    if not base.exists():
        raise ModelLibraryUnavailable(f"mcu {manifest.mcu} is not in the model library")

    overlay = manifest.overlay_path
    if overlay is None:
        return base

    # The platform grammar has no comment syntax — `//` and `#` notes are
    # stripped during resolution — so comment-only lines must go before the
    # fragment is appended.
    body = "\n".join(
        line
        for line in overlay.read_text().splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )
    merged = workdir / f"{manifest.mcu}-overlaid.replx"
    merged.write_text(base.read_text().rstrip() + "\n\n" + body.strip() + "\n")
    return merged
