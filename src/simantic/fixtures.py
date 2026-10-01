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
    """A test.yaml: one machine (`mcu:` at the top level) or several (`machines:`).

    Either form is run as a scenario. Single-machine manifests are the one-machine
    case with the machine named `dut`, which is also the machine name the
    multi-machine form uses by convention in sim-fixtures.
    """

    path: Path
    machines: dict[str, dict]          # name -> {mcu, elf, overlay?}
    media: list[dict]
    nets: list
    timeout: int
    expect: list[str]
    expect_absent: list[str]
    expect_frames: list[str]
    expect_frames_absent: list[str]
    elf_external: bool = False
    quantum: float | None = None
    seed: int | None = None
    serial_execution: bool | None = None

    @property
    def single(self) -> bool:
        return len(self.machines) == 1

    # Single-machine conveniences (the first machine).
    @property
    def _first(self) -> dict:
        return next(iter(self.machines.values()))

    @property
    def mcu(self) -> str:
        return self._first["mcu"]

    @property
    def elf(self) -> str:
        return self._first["elf"]

    @property
    def overlay(self) -> str | None:
        return self._first.get("overlay")

    @property
    def elf_path(self) -> Path:
        return self.path.parent / self.elf

    @property
    def overlay_path(self) -> Path | None:
        return self.path.parent / self.overlay if self.overlay else None

    def scenario(self, workdir: Path) -> dict:
        """The scenario dict `Sim(scenario=...)` takes, with absolute paths.

        Overlay fragments are copied into `workdir` with `{TEST_DIR}` expanded
        to the manifest's directory, the way sim-fixtures' runner does, so a
        fragment's `file:` references resolve wherever pytest is run from.
        """
        here = self.path.parent.resolve()
        machines = {}
        for name, m in self.machines.items():
            spec = {"mcu": m["mcu"], "elf": str(here / m["elf"])}
            if m.get("overlay"):
                text = (here / m["overlay"]).read_text().replace("{TEST_DIR}", str(here))
                frag = workdir / f"{name}-{Path(m['overlay']).name}"
                frag.write_text(text)
                spec["overlay"] = str(frag)
            machines[name] = spec
        scenario: dict = {"machines": machines}
        if self.media:
            scenario["media"] = self.media
        if self.nets:
            scenario["nets"] = self.nets
        if self.quantum is not None:
            scenario["quantum"] = self.quantum
        if self.seed is not None:
            scenario["seed"] = self.seed
        if self.serial_execution is not None:
            scenario["serialExecution"] = self.serial_execution
        return scenario


class UnsupportedManifest(ValueError):
    """The manifest describes a fixture this SDK cannot run yet."""


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    """Parse a test.yaml, or raise UnsupportedManifest with the reason."""
    path = Path(path)
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    if "machines" in data:
        machines = {}
        for name, m in (data["machines"] or {}).items():
            if "mcu" not in m or "elf" not in m:
                raise UnsupportedManifest(f"machine {name!r} needs mcu and elf")
            machines[name] = {k: m[k] for k in ("mcu", "elf", "overlay") if k in m}
        if not machines:
            raise UnsupportedManifest("machines: is empty")
    elif "mcu" in data:
        if "elf" not in data:
            raise UnsupportedManifest("manifest names no elf")
        machines = {"dut": {k: data[k] for k in ("mcu", "elf", "overlay") if k in data}}
    else:
        raise UnsupportedManifest("manifest names no mcu")
    if data.get("hardware"):
        raise UnsupportedManifest("fixture needs physical hardware")
    return Manifest(
        path=path,
        machines=machines,
        media=list(data.get("media") or []),
        nets=list(data.get("nets") or []),
        timeout=int(data.get("timeout", 15)),
        expect=list(data.get("expect") or []),
        expect_absent=list(data.get("expect_absent") or []),
        expect_frames=list(data.get("expect_frames") or []),
        expect_frames_absent=list(data.get("expect_frames_absent") or []),
        elf_external=bool(data.get("elf_external", False)),
        quantum=data.get("quantum"),
        seed=data.get("seed"),
        serial_execution=data.get("serialExecution"),
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
    return platform_path(manifest.mcu, manifest.overlay_path, workdir)


def platform_path(mcu: str, overlay: Path | None, workdir: Path) -> Path:
    """Resolve `mcu` from the local model library, appending `overlay` if given."""
    base = resolved_models() / f"{mcu}.replx"
    if not base.exists():
        raise ModelLibraryUnavailable(f"mcu {mcu} is not in the model library")

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
    merged = workdir / f"{mcu}-overlaid.replx"
    merged.write_text(base.read_text().rstrip() + "\n\n" + body.strip() + "\n")
    return merged
