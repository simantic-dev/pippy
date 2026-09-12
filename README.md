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

> **Alpha, version 0.3.x.** We are still moving things around, so the API can
> change without a deprecation period. Pin an exact version
> (`simantic==0.3.0`) if you depend on it, and please hold off on production
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
`~/.sim_id`. In CI, pipe one in instead: `echo $TOKEN | simantic auth`. If you
bring your own platform file (`repl="board.repl"`), you need no account at all.

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
xdist for free. Multi machine manifests need the `--scenario` runner and report
as skips for now.

For a single run with no assertions in the middle, there is `run_firmware(...)`:

```python
run = simantic.run_firmware("build/zephyr.elf", mcu="STM32F401RE",
                            expect=["RESULT: PASS"])
assert run.passed, run.failure_report()
```

## Telemetry

When you are signed in, we count the shape of a pytest session (how many tests
ran, passed, failed, skipped) and which SDK calls you make, by name only. It is
one request per pytest run, buffered in `~/.simantic/usage.jsonl`, and uploaded
at most hourly, so nothing ever waits on the network.

We do not send file paths, project names, test names, firmware, or simulation
output. Those are yours. Turn it off whenever you like:

```bash
export SIMANTIC_TELEMETRY=0     # or DO_NOT_TRACK=1
```

## Questions

We would genuinely like to hear how this goes for you, especially if something
is confusing or broken. Write to **founder@simantic.dev**, or open an issue.

## License

MIT. The simulators it drives are separate software under their own terms.
