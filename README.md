<p align="center">
  <img src="https://simantic.dev/simantic_logo_4_full.png" alt="Simantic" width="340">
</p>

<h3 align="center">Test your firmware without a board.</h3>

<p align="center">
  <a href="https://pypi.org/project/simantic/"><img src="https://img.shields.io/pypi/v/simantic.svg" alt="PyPI"></a>
  <img src="https://img.shields.io/pypi/pyversions/simantic.svg" alt="Python versions">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT licence">
</p>

Nothing to plug in, nothing to flash. Simantic boots your real ELF on a
simulated microcontroller and hands you the whole machine from Python. Watch it
print, press a button, read a variable straight out of RAM.

```bash
pip install simantic
```

```python
from simantic import Sim

with Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2") as sim:
    sim.expect("ready")
    sim.inject_gpio("gpioc", 13, True)      # press the user button
    sim.expect("button pressed")
    assert sim.read_u32("press_count") == 1
```

That is a whole test. No probe, no breakpoint, no waiting on hardware.

Three things you get that a bench cannot give you:

* **See inside.** Read any variable, register, or RTOS thread while the
  firmware runs, without halting it.
* **Poke it.** Press buttons, send CAN frames, feed the radio, all from your
  script.
* **Repeat exactly.** Time moves only when you ask, so a run comes out the same
  every time, on your laptop and in CI.

The simulator lives inside your Python process, so there is no server to start
and no port to talk to.

> **Alpha, version 0.4.x.** We are still moving things around, so the API can
> change without a deprecation period. Pin an exact version
> (`simantic==0.4.0`) if you depend on it, and please hold off on production
> pipelines for now. Tell us what breaks.

## Setup

`pip install` is the whole setup. The first `Sim(...)` downloads the engine it
needs into `~/.simantic/` and checks it against the published checksum. The
wheel on PyPI holds only Python code; the simulators are never inside it.

Sign in once if you want to name MCUs by part number:

```bash
simantic auth
```

That opens a browser tab, much like `gh auth login`, and saves a token to
`~/.sim_id`. In CI, pipe one in instead: `echo $TOKEN | simantic auth`.

Already have the `sim` binary? Put it on PATH or point `$SIMANTIC_SIM` at it.
`simantic status` shows what resolved.

## Pick your engine

The same script runs on either engine. You choose per simulation:

```python
Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2")                  # Renode engine, the default
Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2", backend="rust")  # our Rust engine
```

The Rust engine is a small extension module, runs one machine, and is
considerably faster. Anything it cannot do yet, such as multi machine scenarios
or CAN and radio injection, raises `simantic.NotSupported` and names the gap
instead of quietly doing nothing. You can follow what each engine covers in
[simantic-core#183](https://github.com/simantic-dev/simantic-core/issues/183).

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

## Testing with pytest

Take the `sim` fixture and write ordinary tests:

```python
def test_timer_irq_fires(sim):
    s = sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2")
    s.expect("fired=1", timeout=8)
    assert s.read_u32("fired") == 1
```

The engine starts once per worker rather than once per test, and every machine
is closed for you. When a test fails, its UART transcript is attached to the
report, because that is usually the evidence you want.

### Test what went over the wire

Firmware that prints "sensor OK" is reporting its own bookkeeping. The bus
tells you what happened. Put a scripted device on the other end of each bus
(a `parts` entry per device, each backed by a few dozen lines of Python), wire
the media in a scenario, and assert on `frames()`:

```python
SCENARIO = {
    "machines": {"dut": {
        "mcu": "STM32H753IITX", "elf": "fc.elf",
        "parts": [
            {"name": "imu0",    "type": "spi-device",  "bus": "spi1", "cs": "PC4", "script": "icm42688.py"},
            {"name": "baro",    "type": "i2c-device",  "bus": "i2c2", "address": 0x63, "script": "icp20100.py"},
            {"name": "gpspeer", "type": "uart-device", "baud": 230400, "script": "gps.py"},
            {"name": "canpeer", "type": "can-node",    "script": "dronecan.py"},
        ],
    }},
    "media": [
        {"type": "uart", "connect": ["dut.usart6", "dut.gpspeer"]},
        {"type": "can",  "connect": ["dut.fdcan1", "dut.canpeer"]},
    ],
}

def test_imu_configured_then_streams(sim):
    s = sim(scenario=SCENARIO, machine="dut", uart="usart1")
    s.run_for(3.0)                                            # virtual seconds
    imu = [f for f in s.frames() if f["label"] == "imu0"]     # one dict per chip-select window
    cfg = next(f for f in imu if f["data"][0] == 0x4F)        # GYRO_CONFIG0 write
    assert cfg["data"][1] & 0x0F == 0x06                      # ODR the driver programmed
    assert cfg["miso"][0] == 0x00                             # what the slave answered
    bursts = [f for f in imu if f["t"] > cfg["t"] and len(f["data"]) > 16]
    assert len(bursts) > 100                                  # FIFO reads after configuration
```

Every record carries its virtual-time stamp, so a rate is a subtraction between
consecutive frames and an ordering is a comparison. The scenario dict is the
same shape as `sim --scenario`'s YAML, and because the test owns it, the
negative control is the same test with a peer removed from `media`. Changing
what a device does (a NACK for 50 ms, a stale frame, a node that stops
answering at t = 5 s) is an edit to the peer script, on the virtual clock. The
`sim` guide covers every output and the peer API:
https://github.com/simantic-dev/simantic-cli/blob/main/docs/GUIDE.md

`--sim-backend=renode|rust|both` chooses the engine. With `both`, each test runs
on each and the engine name appears in the test id. Anything an engine cannot do
is reported as a skip with the reason, so one suite can target both and stay
honest about what each covers.

One tip worth real time: on the Renode engine, every hand off between Python and
the simulation costs a few hundred microseconds. Reading is free, pausing and
resuming is not. Prefer `expect()`, which crosses once, over a loop that polls
every millisecond. On the Rust engine, polling is essentially free.

If you keep `test.yaml` fixture manifests, installing the package also turns
each one into its own pytest item, so you get `-k` filtering, `--junitxml`, and
xdist for free. A manifest is the fire-and-forget form of a test: name the
machine(s) with their `parts`, the `media` that wire UART and CAN peers to
the firmware's controllers, how long to run, and what must and must not
appear:

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
expect_frames:        ["SPI Rx cs=0 len=2 mosi=4F 06"]   # same line shape as `sim --frames`
expect_frames_absent: ["CAN Dropped"]
```

Single-machine manifests (`mcu:` at the top level) are the one-machine case.
Models resolve through your account; no checkout of ours is needed. A model can
require a minimum engine version; if yours is older, the failure names the
version and the fix (`simantic install engine --force`).

Three more keys cover fixtures that need files or peers on the host side:
`sparse_files` creates blank files of a given size before the run (a blank SD
card, say), `networkServices` adds scripted network peers, and `{TEST_DIR}` and
`{WORK_DIR}` in a board file expand to the manifest's directory and a per-test
scratch directory. A manifest whose `sim_args` asks for a `sim`
command-line flag with no equivalent here is skipped, with the flag named.

A run also has a wall-clock budget: 30 s of host time by default, 100 s with
`wireless: true`, or whatever `wall:` says. Going over it fails the test with
the virtual time reached. Slow simulation is a firmware busy-wait or a model
gap, and the report tells you which to go find rather than waiting it out.

For a single run with no assertions in the middle, there is `run_firmware(...)`:

```python
run = simantic.run_firmware("build/zephyr.elf", mcu="STM32F401RE",
                            expect=["RESULT: PASS"])
assert run.passed, run.failure_report()
```

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

We do not send file paths, project names, test names, call arguments, firmware,
or simulation output. Those are yours. Turn it off whenever you like:

```bash
export SIMANTIC_TELEMETRY=0     # or DO_NOT_TRACK=1
```

## Questions

We would genuinely like to hear how this goes for you, especially if something
is confusing or broken. Write to **founder@simantic.dev**, or open an issue.

## License

MIT. The simulators it drives are separate software under their own terms.
