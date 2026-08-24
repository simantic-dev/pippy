<p align="center">
  <img src="https://simantic.dev/simantic_logo_4_full_transparent.png" alt="Simantic" width="340">
</p>

<p align="center">
  <b>Run your firmware in a simulator, from Python.</b><br>
  No board, no debugger, no wiring.
</p>

Simantic simulates the microcontroller your firmware runs on. You boot a real
ELF, watch its UART, press a button, and read a variable straight out of
memory, all from a Python script or a pytest suite.

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

Time moves only when you ask it to, so a run is repeatable and your think time
is free. The simulator lives inside your Python process, so there is no server
to start and no port to talk to.

> **Alpha, version 0.3.x.** The API may change without a deprecation period.
> Pin an exact version (`simantic==0.3.0`) if you depend on it, and please do
> not put it in a production pipeline yet.

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
considerably faster. Anything it cannot do yet, such as multi machine
scenarios or CAN and radio injection, raises `simantic.NotSupported` and names
the gap instead of quietly doing nothing. You can follow what each engine
covers in [simantic-core#183](https://github.com/simantic-dev/simantic-core/issues/183).

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

`--sim-backend=renode|rust|both` chooses the engine. With `both`, each test
runs on each and the engine name appears in the test id. Anything an engine
cannot do is reported as a skip with the reason, so one suite can target both
and stay honest about what each covers.

One tip that is worth real time: on the Renode engine, every hand off between
Python and the simulation costs a few hundred microseconds. Reading is free,
pausing and resuming is not. Prefer `expect()`, which crosses once, over a loop
that polls every millisecond. On the Rust engine polling is essentially free.

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
