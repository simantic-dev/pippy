"""`simantic demo`: run a packaged scenario without an account.

The first thing someone does after `pip install simantic` — or after
`uvx simantic demo`, which installs nothing permanent. Nothing here reads
`~/.sim_id` or touches `get-mcu-details`: a demo's platform and firmware come
from the public releases bucket, so it runs before a reader has decided
whether to sign up.

Scenarios are multi-machine, which is the point. A single chip printing to a
UART does not show anything a reader cannot already get elsewhere; two of them
finding each other over a simulated radio does. The run is delegated to the
`sim` binary's `--scenario`, which already wires N machines onto one shared
medium and one virtual timeline, rather than reimplemented against the
single-machine `simantic_rust.Session`.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import install
from ._locate import BinaryNotFound, locate
from .mcu import BINARY as SIM_BINARY
from .mcu import ENV_VAR as SIM_ENV


class DemoError(RuntimeError):
    """A demo could not be prepared or run."""


@dataclass(frozen=True)
class Demo:
    """One packaged scenario: its assets, and the scenario file to write."""

    name: str
    summary: str
    #: Asset file name -> path under the demo prefix in the releases bucket.
    assets: dict[str, str]
    #: `scenario.yaml` contents, less the paths, which are filled from assets.
    machines: dict[str, dict[str, str]]
    media: list[dict[str, object]] = field(default_factory=list)
    quantum: float | None = None
    timeout: int = 30


#: BLE's inter-frame spacing is 150 us, so the two nodes have to interleave
#: well inside that or one misses the other's response window entirely.
BLE_QUANTUM_S = 0.00001

DEMOS: dict[str, Demo] = {
    "ble-pair": Demo(
        name="ble-pair",
        summary=(
            "two ESP32-C3s running stock ESP-IDF bleprph and blecent, "
            "pairing over a simulated BLE medium"
        ),
        assets={
            "esp32c3.repl": "ble-pair/esp32c3.repl",
            "bleprph.bin": "ble-pair/bleprph-flash.bin",
            "blecent.bin": "ble-pair/blecent-flash.bin",
        },
        machines={
            "peripheral": {"repl": "esp32c3.repl", "elf": "bleprph.bin"},
            "central": {"repl": "esp32c3.repl", "elf": "blecent.bin"},
        },
        media=[{"type": "ble", "connect": ["peripheral.radio", "central.radio"]}],
        quantum=BLE_QUANTUM_S,
        timeout=30,
    ),
}

#: Demo assets are published beside the release manifests, under their own
#: prefix so a demo can be re-cut without a new engine release.
DEMO_PRODUCT = "demos"


def demo_root() -> Path:
    """Where fetched demo assets live: ~/.simantic/demos/<name>/."""
    return install.simantic_home() / "demos"


def _manifest(timeout: float = 30) -> dict:
    """The demo asset manifest: {"assets": {path: {"sha256": ...}}}."""
    url = f"{install.releases_url()}/{DEMO_PRODUCT}/manifest.json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise DemoError(
            f"no demo assets published (HTTP {exc.code} from {url})"
        ) from None
    except urllib.error.URLError as exc:
        raise DemoError(f"cannot reach the release server: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise DemoError(f"demo manifest is not valid JSON: {exc}") from None


def fetch_assets(demo: Demo, *, force: bool = False) -> Path:
    """Download the demo's assets into its cache directory, and return it."""
    target = demo_root() / demo.name
    missing = [n for n in demo.assets if force or not (target / n).exists()]
    if not missing:
        return target

    entries = _manifest().get("assets") or {}
    target.mkdir(parents=True, exist_ok=True)
    for name in missing:
        remote = demo.assets[name]
        entry = entries.get(remote)
        if entry is None:
            raise DemoError(f"{remote!r} is not in the demo manifest")
        artifact = install.Artifact(
            version=demo.name,
            url=f"{install.releases_url()}/{DEMO_PRODUCT}/{remote}",
            sha256=entry.get("sha256"),
        )
        try:
            payload = install.download(artifact)
        except install.InstallError as exc:
            raise DemoError(f"{name}: {exc}") from None
        (target / name).write_bytes(payload)
    return target


def write_scenario(demo: Demo, directory: Path) -> Path:
    """Write the scenario file `sim --scenario` reads, beside the assets.

    Paths stay bare names because `sim` resolves `repl` and `elf` relative to
    the scenario file's own directory.
    """
    scenario: dict[str, object] = {
        "machines": demo.machines,
        "timeout": demo.timeout,
    }
    if demo.media:
        scenario["media"] = demo.media
    if demo.quantum is not None:
        scenario["quantum"] = demo.quantum
    path = directory / "scenario.yaml"
    # The reader is a serde_yaml Deserialize, and JSON is a subset of YAML, so
    # this avoids a PyYAML dump and its tag/anchor surprises.
    path.write_text(json.dumps(scenario, indent=2) + "\n")
    return path


def run(name: str, *, force: bool = False, extra: list[str] | None = None) -> int:
    """Prepare and run a demo, streaming the simulators' output."""
    demo = DEMOS.get(name)
    if demo is None:
        known = ", ".join(sorted(DEMOS))
        raise DemoError(f"unknown demo {name!r}; available: {known}")

    try:
        binary = locate(SIM_BINARY, SIM_ENV)
    except BinaryNotFound:
        try:
            binary = install.install(SIM_BINARY)
        except install.InstallError as exc:
            raise DemoError(
                f"the {SIM_BINARY!r} binary is needed to run a scenario and "
                f"could not be installed: {exc}"
            ) from None

    directory = fetch_assets(demo, force=force)
    scenario = write_scenario(demo, directory)
    command = [str(binary), "--scenario", str(scenario), *(extra or [])]
    try:
        return subprocess.call(command)
    except OSError as exc:
        raise DemoError(f"could not run {binary}: {exc}") from None
