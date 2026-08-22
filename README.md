# simantic

Python control of the [Simantic](https://simantic.dev) simulators — the
firmware engine hosted in your process, circuits via `analog-cli`. Everything
the CLIs can do, as objects and method calls: start a board or a multi-machine scenario, advance virtual
time by exact amounts, inject UART/GPIO/CAN/radio, read memory and RTOS state,
and run as many simulations in parallel as you have cores. pytest is one way
to use it, not a requirement.

> **Alpha — not stable.** Version 0.1.x. The API, the CLI surface, and the
> report schema may change without a deprecation period, and any release may
> break the previous one. Pin an exact version (`simantic==0.1.0`) if you
> depend on it. Not recommended for production pipelines yet.

> **A Simantic account is required.** Installing the package gets you the
> Python code, but the simulators it drives are fetched from our backend and
> every request is authenticated. Without `simantic auth`, nothing runs.

```bash
pip install simantic
simantic auth              # required: authenticates against your account
simantic install           # fetch the simulator binaries
```

`simantic auth` opens a browser tab to sign in — like `gh auth login` — and
stores the resulting token in `~/.sim_id`, the same file the CLIs use, so one
login covers all of them. In a script or CI, pass `--token` or pipe one in
(`echo $TOKEN | simantic auth`) instead of opening a browser. Create a token
on the dashboard's `/account/api` page. `--no-browser` falls back to an
interactive prompt for a pasted token.

`simantic install` then downloads the binaries into `~/.simantic/bin`,
verifying each against the checksum in the release manifest, and the SDK
finds them there with no further configuration. It fails closed: with no
stored credentials it stops before any download and tells you to
authenticate.

Already have the binaries? Point `$SIMANTIC_ANALOG_CLI` and `$SIMANTIC_SIM`
at them, or put them on PATH — both take precedence over a managed install.
`simantic status` shows what is authenticated and which binary each name
resolves to.

## Drive a simulation

A `Sim` is a live simulation you control. Time advances only when you ask, so
a script is deterministic and your think-time is free:

```python
from simantic import Sim

with Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2") as sim:
    sim.expect("ready")
    sim.inject_gpio("gpioc", 13, True)      # press the user button
    m = sim.expect("button pressed")
    assert m.virtual_seconds < 0.010        # within 10 virtual ms
    assert sim.read_u32("press_count") == 1
```

The same class runs multi-machine scenarios with scripted peers
(`Sim(scenario={...})`). The engine lives in your process (one emulation per
process), so a parameter sweep is a `ProcessPoolExecutor` over plain
functions. See
[docs/session-api.md](docs/session-api.md) and `examples/`.

One-shot runs ("run 5 s, give me the transcript") are `run_firmware(...)`.

## Using it from pytest (optional)

`Sim` needs no plugin — construct it inside any test. If you also keep
manifests, installing the package registers two collectors that turn them
into individually addressable pytest items:

- `*.sim.toml` — one item per `[[test]]` table (analog)
- `test.yaml` — one item per fixture (firmware)

```console
$ pytest hardware/ firmware/
hardware/psu/psu.sim.toml::schematic-erc                PASSED
hardware/psu/psu.sim.toml::rails-op                     PASSED
hardware/psu/psu.sim.toml::startup-settling             FAILED
hardware/psu/psu.sim.toml::board-drc                    SKIPPED (no .kicad_pcb)
firmware/tests/gpio-loopback/test.yaml::gpio-loopback   PASSED
```

Because these are ordinary pytest items you get `-k` filtering, `--junitxml`
for CI, xdist parallelism, and per-test durations. Failures print the
runner's own explanation rather than a Python traceback:

```
startup-settling (tran): fail
  FAIL settle-time: V(OUT) measured 0.0082 (expected max 0.006, margin -0.0022)
```

Tests that cannot run in the current environment skip rather than fail — a
missing binary, an unconfigured server, an analysis the installed CLI does
not support, a check inapplicable to the project. A red run means a
simulation ran and disagreed with its expectations.

## Library

### Circuits

```python
import simantic

report = simantic.run_tests("hardware/psu")
print(f"{report.summary.passed}/{report.summary.total} passed")

for m in report.test("rails-op").measurements:
    print(m.describe())   # out-dc: V(OUT) measured 1.597 (expected eq 1.597 +/- 0.02, margin 0.02)
```

A failing test is data, not an exception: it arrives in the report with its
measured value, declared bounds, and margin. Only conditions that prevent a
run at all — bad project, missing `kicad-cli`, invalid testplan — raise
`AnalogCliError`.

### Firmware

The shortest path is a pytest fixture — no manifest, no flags:

```python
def test_firmware_boots(pyrite):
    run = pyrite("build/zephyr.elf", board="stm32f401",
                 expect=["Hello World!"], expect_absent=["FAULT"])
    assert run.passed, run.failure_report()
```

`pyrite` runs the ELF offline on the pure-Rust backend and hands back the
UART transcript. The fixture skips when no binary is installed, so a suite
stays green on a machine that has not run `smtc install pyrite`.

The same runner is available as a plain function, and `sim` has its own:

```python
run = simantic.run_firmware(
    "build/zephyr.elf",
    mcu="STM32F401RE",          # resolved by the backend through your account
    expect=["RESULT: PASS"],
    expect_absent=["RESULT: FAIL"],
)
assert run.passed, run.failure_report()
```

`sim` emits no structured report — the only observable is UART text — so the
verdict is substring matching, the same contract `test.yaml` manifests use.
Pass `repl=` instead of `mcu=` for a platform file you author yourself.

MCU models are not distributed with this package: `mcu=` resolves them
through your account. If you have a local model library, set
`$SIMANTIC_MCU_LIB` to resolve from it instead — which is also what applying
a fixture's `overlay` fragment requires.

Some installations need a separate simulation server. When one does, the SDK
raises `ServerNotConfigured` and the pytest plugin skips, rather than
reporting a firmware failure.

## Telemetry

When you are authenticated, a completed pytest session reports its **shape**
to your account: how many simulator tests ran, how many passed, failed, or
skipped, plus this package's version, your Python version, OS, and CPU
architecture. One request per `pytest` invocation, never per test.

It also counts **which calls you make** — SDK functions, MCP tool names, and
`smtc` subcommands, by name only. These are buffered in
`~/.simantic/usage.jsonl` and uploaded as counts at most once an hour, so no
simulation ever waits on the network. You can read that file at any time; it
is one JSON object per line and contains nothing but call names.

It does **not** send file paths, project names, test names, firmware, or
simulation output. Those are yours.

```bash
export SIMANTIC_TELEMETRY=0     # or DO_NOT_TRACK=1
```

`smtc status` prints exactly what is sent and whether it is on. Reporting is
best-effort: if it fails, is blocked, or you are offline, your tests are
unaffected and nothing is printed.

## Compatibility

Speaks the `analog-cli.test-report/1` schema. Additive fields within that
revision are tolerated; a breaking revision raises `ReportError` rather than
silently misreading a report.

Multi-machine `test.yaml` fixtures — those with a `machines:` map — need the
`--scenario` runner and are not driven yet; they report as skips.

## License

MIT. The simulators it drives are separate software under their own terms.
