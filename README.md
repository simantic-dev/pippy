# simantic

Python SDK and pytest plugin for the [Simantic](https://simantic.dev)
simulators — circuits via `analog-cli`, firmware via `sim`.

One package covers both, because co-simulation puts them together: an
`analog-cli` testplan can already declare a `firmware` test with an `elf`.

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

`simantic auth` stores a personal access token in `~/.sim_id` — the same file
the CLIs use, so one login covers all of them. Create a token on the
dashboard's `/account/api` page.

`simantic install` then downloads the binaries into `~/.simantic/bin`,
verifying each against the checksum in the release manifest, and the SDK
finds them there with no further configuration. It fails closed: with no
stored credentials it stops before any download and tells you to
authenticate.

If your shell reports `simantic: command not found`, the launcher pip
generated is in an environment directory that is not on your PATH (most often
on Windows). `python -m simantic ...` is equivalent and needs only an
interpreter that can import the package.

Already have the binaries? Point `$SIMANTIC_ANALOG_CLI` and `$SIMANTIC_SIM`
at them, or put them on PATH — both take precedence over a managed install.
`simantic status` shows what is authenticated and which binary each name
resolves to.

## pytest plugin

Installing the package registers two collectors. The manifests your project
already maintains become individually addressable pytest items:

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

## Compatibility

Speaks the `analog-cli.test-report/1` schema. Additive fields within that
revision are tolerated; a breaking revision raises `ReportError` rather than
silently misreading a report.

Multi-machine `test.yaml` fixtures — those with a `machines:` map — need the
`--scenario` runner and are not driven yet; they report as skips.

## License

MIT. The simulators it drives are separate software under their own terms.
