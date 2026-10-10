"""test.yaml fixture manifests: loading.

A fixture names an MCU (`mcu: STM32F401RE`), not a platform file. The engine
resolves that name against your account; models are not distributed with
this package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

#: Default wall-clock budgets for a manifest run (seconds). Radios cost more.
WALL_WIRED_S = 30
WALL_WIRELESS_S = 100


@dataclass(frozen=True)
class Manifest:
    """A test.yaml: one machine (`mcu:` at the top level) or several (`machines:`).

    Either form is run as a scenario. Single-machine manifests are the one-machine
    case with the machine named `dut`, which is also the machine name the
    multi-machine form uses by convention in sim-fixtures.
    """

    path: Path
    machines: dict[str, dict]          # name -> {mcu, elf, parts?, overlay?}
    media: list[dict]
    nets: list
    timeout: int
    expect: list[str]
    expect_absent: list[str]
    expect_frames: list[str]
    expect_frames_absent: list[str]
    elf_external: bool = False
    wall: int | None = None            # wall-clock budget in seconds; None = default
    wireless: bool = False             # radio fixtures get the longer default budget
    quantum: float | None = None
    seed: int | None = None
    serial_execution: bool | None = None
    network_services: list = ()        # passed through as the scenario's networkServices
    sparse_files: dict = None          # name -> size ("4G"); blank files made in the work dir

    @property
    def single(self) -> bool:
        return len(self.machines) == 1

    @property
    def wall_budget(self) -> int:
        """Seconds of host time the run may take. Slow simulation is a model
        gap or a firmware busy-wait, not a fact of life, so a run past this
        budget fails rather than waits. Same defaults as sim-fixtures' runner."""
        if self.wall is not None:
            return self.wall
        return WALL_WIRELESS_S if self.wireless else WALL_WIRED_S

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
        to the manifest's directory and `{WORK_DIR}` to `workdir`, the way
        sim-fixtures' runner does, so a fragment's `file:` references resolve
        wherever pytest is run from. `sparse_files:` are created blank in
        `workdir` (a card image the guest formats cannot be committed at size).
        """
        here = self.path.parent.resolve()
        for name, size in (self.sparse_files or {}).items():
            with open(workdir / str(name), "wb") as f:
                f.truncate(_parse_size(size))
        machines = {}
        for name, m in self.machines.items():
            spec = {"mcu": m["mcu"], "elf": str(here / m["elf"])}
            if m.get("overlay"):
                text = ((here / m["overlay"]).read_text().replace("{TEST_DIR}", str(here))
                        .replace("{WORK_DIR}", str(workdir)))
                frag = workdir / f"{name}-{Path(m['overlay']).name}"
                frag.write_text(text)
                spec["overlay"] = str(frag)
            if m.get("parts"):
                spec["parts"] = [_resolve_paths(p, here) for p in m["parts"]]
            machines[name] = spec
        scenario: dict = {"machines": machines}
        if self.media:
            scenario["media"] = self.media
        if self.nets:
            scenario["nets"] = self.nets
        if self.network_services:
            scenario["networkServices"] = list(self.network_services)
        if self.quantum is not None:
            scenario["quantum"] = self.quantum
        if self.seed is not None:
            scenario["seed"] = self.seed
        if self.serial_execution is not None:
            scenario["serialExecution"] = self.serial_execution
        return scenario


class UnsupportedManifest(ValueError):
    """The manifest describes a fixture this SDK cannot run yet."""



def _parse_size(spec) -> int:
    """`4G`, `512M`, `64K` or a bare byte count."""
    text = str(spec).strip().upper()
    unit = {"K": 10, "M": 20, "G": 30, "T": 40}.get(text[-1:], 0)
    return int(text[:-1] if unit else text) << unit


# `sim_args:` flags that change nothing this package asserts on.
_HARMLESS_SIM_ARGS = {"--ascii"}


def _resolve_paths(part: dict, base: Path) -> dict:
    """Peer scripts and card images are given relative to the manifest / scenario."""
    return dict(part, **{k: str(base / part[k]) for k in ("script", "image") if k in part})

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
            machines[name] = {k: m[k] for k in ("mcu", "elf", "overlay", "parts") if k in m}
        if not machines:
            raise UnsupportedManifest("machines: is empty")
    elif "mcu" in data:
        if "elf" not in data:
            raise UnsupportedManifest("manifest names no elf")
        machines = {"dut": {k: data[k] for k in ("mcu", "elf", "overlay", "parts") if k in data}}
    else:
        raise UnsupportedManifest("manifest names no mcu")
    if data.get("hardware"):
        raise UnsupportedManifest("fixture needs physical hardware")
    # A flag of the `sim` binary has no effect on an in-process run, and a
    # fixture that depends on one (a USB console, say) would fail for that
    # reason alone.
    flags = [a for a in map(str, data.get("sim_args") or []) if a.startswith("--") and a not in _HARMLESS_SIM_ARGS]
    if flags:
        raise UnsupportedManifest(f"fixture needs `sim` flags this package does not apply: {' '.join(flags)}")
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
        wall=int(data["wall"]) if data.get("wall") is not None else None,
        wireless=bool(data.get("wireless", False)),
        quantum=data.get("quantum"),
        seed=data.get("seed"),
        serial_execution=data.get("serialExecution"),
        network_services=list(data.get("networkServices") or []),
        sparse_files=dict(data.get("sparse_files") or {}),
    )
