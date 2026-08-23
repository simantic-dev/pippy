# simantic

Python control of the [Simantic](https://simantic.dev) firmware simulator,
hosted in your process. Everything the `sim` CLI can do, as objects and method calls: start a board or a multi-machine scenario, advance virtual
time by exact amounts, inject UART/GPIO/CAN/radio, read memory and RTOS state,
and run as many simulations in parallel as you have cores. pytest is one way
to use it, not a requirement.

> **Alpha — not stable.** Version 0.2.x. The API, the CLI surface, and the
> report schema may change without a deprecation period, and any release may
> break the previous one. Pin an exact version (`simantic==0.2.0`) if you
> depend on it. Not recommended for production pipelines yet.

```bash
pip install simantic
```

That is the whole setup for Python. The first `Sim(...)` fetches the
simulation engine (Simantic.Core plus a private .NET runtime — nothing else
to install) into `~/.simantic/engine/<version>/`, checksum-verified against
the public release manifest. `simantic install` fetches it up front, along
with the `sim` binary if you also want the command-line tool.

A Simantic account (`simantic auth`) is needed for one thing: resolving MCU
models by name (`mcu="STM32F401RE"`), which are fetched from your account
and cached in `~/.sim_cache`. A platform file you supply (`repl=`) needs no
account at all.

`simantic auth` opens a browser tab to sign in — like `gh auth login` — and
stores the resulting token in `~/.sim_id`, the same file the CLIs use, so one
login covers all of them. In a script or CI, pass `--token` or pipe one in
(`echo $TOKEN | simantic auth`) instead of opening a browser. Create a token
on the dashboard's `/account/api` page. `--no-browser` falls back to an
interactive prompt for a pasted token.

Every download — engine or binary — is verified against the checksum in the
release manifest. The package on PyPI contains only Python; the simulators
are never in the wheel.

Already have `sim`? Point `$SIMANTIC_SIM` at it, or put it on PATH — both
take precedence over a managed install. `simantic status` shows what is
authenticated and what resolved.

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
`test.yaml` fixture manifests, installing the package registers a collector
that turns each fixture into an individually addressable pytest item.

```console
$ pytest firmware/
firmware/tests/gpio-loopback/test.yaml::gpio-loopback   PASSED
firmware/tests/uart-echo/test.yaml::uart-echo           FAILED
```

Because these are ordinary pytest items you get `-k` filtering, `--junitxml`
for CI, xdist parallelism, and per-test durations. Failures print the
runner's own explanation — the UART transcript and the expectation it
missed — rather than a Python traceback.

Tests that cannot run in the current environment skip rather than fail — a
missing binary or an unconfigured server. A red run means a
simulation ran and disagreed with its expectations.

## Library

The one-shot runner:

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

Multi-machine `test.yaml` fixtures — those with a `machines:` map — need the
`--scenario` runner and are not driven yet; they report as skips.

## License

MIT. The simulators it drives are separate software under their own terms.
