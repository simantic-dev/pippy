<p align="center">
  <img src="https://simantic.dev/simantic_logo_4_full.png" alt="Simantic" width="340">
</p>

<h3 align="center">Test your firmware without a board.</h3>

<p align="center">
  <a href="https://pypi.org/project/simantic/"><img src="https://img.shields.io/pypi/v/simantic.svg" alt="PyPI"></a>
  <img src="https://img.shields.io/pypi/pyversions/simantic.svg" alt="Python versions">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT licence">
</p>

The `sim` binary is the simulator. This package installs it, runs it, and turns
its output into Python objects and pytest results. Everything the binary does
(several machines, Python scripted peers, media and bridges, `--record`,
`--trace-symbol`, `--real-time`) works from Python because Python just runs it.

```bash
pip install simantic
```

```python
import simantic

run = simantic.run_firmware("fw.elf", mcu="STM32F401RE", expect=["ready"])
assert run.passed, run.failure_report()
```

That is a whole test. No probe, no breakpoint, no waiting on hardware. Time is
virtual, so a run comes out the same every time, on your laptop and in CI.

> **Alpha, version 0.5.x.** 0.5.0 is breaking: the Renode/C# engine is gone and
> pyrite (the Rust `sim` binary) is the only engine. The API can still change
> without a deprecation period, so pin an exact version.

## Setup

`pip install` is the whole setup. The first run downloads the `sim` binary into
`~/.simantic/` and checks it against the published checksum. The wheel on PyPI
holds only Python code.

Sign in once if you want to name MCUs by part number:

```bash
simantic auth
```

That opens a browser tab, much like `gh auth login`, and saves a token to
`~/.sim_id`. In CI, pipe one in instead: `echo $TOKEN | simantic auth`.

Already have the `sim` binary? Put it on PATH or point `$SIMANTIC_SIM` at it.
`simantic status` shows what resolved.

## Run a scenario

`run_scenario` runs `sim run --scenario ... --record DIR` and reads the recorded
bundle back. A scenario can hold several machines, Python scripted peers
(`parts`) and the media that join them:

```python
scenario = {
    "machines": {
        "dut":  {"mcu": "STM32H753IITX", "elf": "fc.elf",
                 "parts": [{"name": "gpspeer", "type": "uart-device",
                            "baud": 230400, "script": "gps.py"}]},
        "node": {"mcu": "STM32F407VG", "elf": "node.elf"},
    },
    "media": [
        {"type": "uart", "connect": ["dut.usart6", "dut.gpspeer"]},
        {"type": "can",  "connect": ["dut.fdcan1", "node.can1"]},
    ],
}
run = simantic.run_scenario(scenario, timeout=12, args=["--trace-symbol", "can_send"])

assert "[dut] GPS fix: 3" in run.output          # "[machine] "-prefixed UART lines
sends = [l for l in run.stdout.splitlines() if l.startswith("[node]") and "can_send" in l]
```

The scenario dict is the same shape as `sim run --scenario`'s YAML. Records
carry virtual-time stamps, so a rate is a subtraction and an ordering is a
comparison. Changing what a peer does (a NACK for 50 ms, a stale frame, a node
that stops answering at t = 5 s) is an edit to its script. The `sim` guide
covers every output and the peer API: https://simantic.com/docs

## Testing with pytest

Installing the package turns each `test.yaml` manifest into its own pytest item,
so you get `-k` filtering, `--junitxml` and xdist. A manifest is a scenario plus
what must and must not appear:

```yaml
machines:
  dut:
    mcu: STM32H753ZI
    elf: fc.elf
    parts:
      - { name: gpspeer, type: uart-device, baud: 230400, script: gps.py }
media:
  - { type: uart, connect: [dut.usart6, dut.gpspeer] }
timeout: 12
expect:               ["GPS fix: 3"]
expect_absent:        ["RESULT: FAIL"]
```

```bash
pytest path/to/fixtures -k gps
```

Each item runs `sim run --scenario ... --record DIR` and checks the bundle. When
a test fails, its UART transcript is attached to the report. Single-machine
manifests (`mcu:` at the top level) are the one-machine case. Models resolve
through your account; no checkout of ours is needed.

A run has a wall-clock budget: 30 s of host time by default, or whatever `wall:`
says. Going over it fails the test with the virtual time reached. Slow
simulation is a firmware busy-wait or a model gap, and the report says which.

For a scenario with Python assertions in the middle, call `run_scenario` from an
ordinary test, as above; see [docs/writing-tests.md](docs/writing-tests.md).

## Stepping one machine: `Sim`

`Sim` runs the in-process pyrite engine on a single machine, so you can step
virtual time, inject, and read state between steps. It is a debugging and
stepping layer, not the way to run scenarios or suites.

```python
from simantic import Sim

with Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2") as sim:
    sim.expect("ready")
    sim.inject_gpio("gpioc", 13, True)      # press the user button
    sim.expect("button pressed")
    assert sim.read_u32("press_count") == 1
```

Time moves only when you ask. See [docs/session-api.md](docs/session-api.md).

## When to use what

* **The `sim` CLI directly:** one-off runs, `--real-time`, `--uart-tcp` bridges,
  `--record` bundles, interactive poking.
* **`run_scenario` and pytest `test.yaml`:** anything with several machines,
  scripted peers or media, and every suite that runs in CI.
* **`Sim`:** one machine, when you need to pause part-way, inject input, or read
  memory and RTOS state between steps.

## ESP32 images

The ESP32-C3, C6 and P4 boot the way silicon does: Espressif's mask ROM runs
first, then your bootloader, then your app. `sim --elf` therefore takes one ELF
that carries the ROM and your whole flash image. Build it from the files your
ESP-IDF or PlatformIO build already produced:

```bash
simantic esp-image --chip esp32c3 \
    --part 0x0:bootloader.bin --part 0x8000:partitions.bin --part 0x10000:firmware.bin \
    --flash-size 16MB -o image.elf
sim --elf image.elf ...
```

Already have a merged image from `esptool.py merge_bin`? Pass `--flash merged.bin`
instead of the parts. The same thing from Python is
`simantic.esp_image.build_image("esp32c3", flash, out="image.elf")`.

The mask ROM is Espressif's and is not bundled. On first use it is downloaded
from Espressif's own repositories, pinned by commit and SHA-256, and cached in
`~/.simantic/esp-rom`: the raw C3 and C6 dumps from
[espressif/qemu](https://github.com/espressif/qemu/tree/master/pc-bios), and
the P4 ROM from the [esp-rom-elfs](https://github.com/espressif/esp-rom-elfs)
release ESP-IDF installs. Offline, or with your own copy, pass `--rom`.
`simantic esp-rom --chip esp32c3` downloads it ahead of time and prints where it
went.

## Telemetry

Only when you are signed in, we report two things:

* **Test counts.** At the end of a pytest run, how many simulator tests passed,
  failed and were skipped. One request per run.
* **Which calls you use.** The names of the SDK calls and `simantic` commands
  you run, such as `sdk.run_firmware` or `cli.install`, and how often. They are
  counted in `~/.simantic/usage.jsonl` and uploaded at most hourly.

Each report also carries the version of this package, your Python version,
operating system and CPU architecture. Reports go out when a pytest run or a
`simantic` command finishes, never while a simulation is running, and a failed
or slow request is dropped silently.

## Questions

We would genuinely like to hear how this goes for you, especially if something
is confusing or broken. Write to **founder@simantic.dev**, or open an issue.

## License

MIT. The simulators it drives are separate software under their own terms.
