# Driving the engine: `simantic.Sim`

`Sim` is the Python face of the simulator — the same capabilities as the
`sim` CLI, as a live object. No test framework is involved; pytest, a plain
script, a notebook or a process pool all use it the same way. It builds the
machine(s) inside your Python process and holds them at reset; every method either
advances virtual time by an exact, requested amount or observes state without
advancing it. Between calls nothing runs, so a script is deterministic and
Python think-time is free.

It is the same class for a single board and for a multi-machine scenario with
radio/CAN/Ethernet media and scripted network peers — everything `sim
--scenario` accepts, `Sim(scenario=...)` accepts.

## Start

```python
from simantic import Sim

# a model name: ~/.sim_cache, else fetched with your credentials and cached (like `sim --mcu`)
Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2",
    parts=[{"name": "baro", "type": "i2c-device", "bus": "i2c1", "address": 0x76, "script": "baro.py"}])

# a scenario dict — the sim --scenario schema as Python
Sim(scenario={...}, machine="c6", uart="uart0", cwd=fixture_dir)
```

Scenario dicts use the `sim --scenario` schema verbatim: `machines`
(`mcu` + `parts` + `overlay` + `elf` + `symbolsElfPath`), `media` (BLE/CAN/Ethernet/UART
buses), `networkServices` (scripted peers), `quantum`. Relative paths resolve
against `cwd=` (default: the process cwd).

`parts` is a list of devices on the board, one dict each: `name`, `type`, then
per type — `uart-device` (`script`, `baud`; wire it with a uart medium),
`i2c-device` (`bus`, `address`, `script`), `spi-device` (`bus`, `script`, `cs`:
an index, or a pin like `"PC4"` when several devices share the bus),
`can-node` (`script`; wire with a can medium), `ble-peer` / `wifi-peer`
(`ssid`, `passphrase`) / `eth-peer` (`mac`) (`script`; wire with the matching
medium), `sd-card` (`bus`, `size` such as `"16MiB"`, `image`: a formatted card image, since a blank card has nothing to mount), `peripheral` (`address`,
`size`, `script`, optional `irq` list), `model` (a stock part: `class` such as
`Sensors.BMP388`, `bus`, `address`, `properties`).
`overlay` is a board file (`.board`), a raw platform fragment for anything that
list cannot express. Needs engine 0.5.17+.

A model can require a newer engine than the one installed (the STM32H7 parts
need 0.6.3). `Sim` then raises `SimError` naming the version it needs; update
with `simantic install engine --force`.

`symbols_elf=` (single-machine form) / `symbolsElfPath` (scenario machine
entries — same key as the CLI's scenario YAML, so a scenario dict is
copy-pasteable) attaches a companion ELF that carries debug symbols for a
stripped image, e.g. a PlatformIO/IDF `firmware.elf` alongside a stripped
flash container passed as `elf=`. It only changes what `symbol()` and RTOS
introspection can resolve — the image that actually runs is still `elf=`.

`trace_symbols=[...]`, `trace_memory=[...]`, `trace_interrupts=True` and
`show_logs=True` turn on the non-halting instrumentation; read it back with
`symbol_trace()`, `interrupts()` and `logs()`.

## Drive

| method | advances time | returns |
| --- | --- | --- |
| `expect(pattern, timeout=30)` | until the regex matches (or `timeout` wall seconds) | `Match(text, virtual_seconds)`; raises `ExpectTimeout` (an `AssertionError`) |
| `run_for(seconds)` | exactly `seconds` | elapsed virtual time |
| `send(text)` / `send_bytes(b)` | no — delivered when time next advances | |
| `inject_gpio(peripheral, pin, state)` | no | |
| `inject_can(peripheral, id, data)` / `inject_radio(peripheral, frame)` | no | |

`expect` keeps a pexpect-style stream: output that arrived in a previous
call's overshoot is matched first, so a burst of lines can be expected one by
one. `timeout` is wall-clock effort, not virtual time — assert on
`Match.virtual_seconds` when the claim is about timing.

## Observe (never advances time)

| method | returns |
| --- | --- |
| `time` | elapsed virtual seconds |
| `read_uart()` / `read_uart_bytes()` / `uart_records()` | text / raw bytes since the last read / `[{t, machine, label, text, bytes}]` |
| `frames()` | `[{t, machine, label, protocol, direction, summary, id, data, miso}]` — SPI/I2C/CAN/BLE/Ethernet; `data` is the payload (MOSI for SPI), `miso` the SPI reply bytes (None otherwise; needs engine ≥ 0.5.17) |
| `logs()` | `[{t, level, source, message}]` — unhandled registers, model warnings |
| `interrupts()` | `[{t, machine, direction, exception, name}]` (with `trace_interrupts=True`) |
| `symbol_trace()` | `[{t, machine, symbol, address, args}]` (with `trace_symbols=[...]`) |
| `memory_trace()` | `[{t, machine, watch, kind, address, value}]` (with `trace_memory=[...]`; `value` is `None` for a read). `backend="rust"` only |
| `itm()` | `[{t, machine, port, bytes, text}]`: ITM stimulus-port output (with `itm=True`, Cortex-M). `backend="rust"` only |
| `read_memory(addr_or_symbol, count)` / `read_u32(...)` | bytes / int from the system bus (RAM, flash, peripheral registers) |
| `symbol(name)` | ELF symbol address |
| `threads()` / `heap()` | RTOS thread snapshot / heap report, when recognised |

Every observer takes `machine=` in a scenario; the constructor's `machine=`
and `uart=` are the defaults.

## What each engine supports

| | `backend="renode"` | `backend="rust"` |
| --- | --- | --- |
| UART text: `expect()`, manifest `expect` / `expect_absent` | yes | yes |
| Bus frames (SPI, I2C, CAN, BLE, Ethernet): `frames()`, manifest `expect_frames` | yes | no: `frames()` is empty, a manifest with `expect_frames` is skipped |
| `interrupts()` | yes | yes, `Sim` and `run_scenario` |
| `symbol_trace()` | yes | yes, `Sim` and `run_scenario` |
| `memory_trace()` | no | yes, `Sim` and `run_scenario` |
| `itm()` | no | yes, `Sim` only |
| `read_memory()` / `read_u32()` / `symbol()` | yes | yes, `Sim` only |
| `threads()` / `heap()` | yes | yes, `Sim` only |
| `logs()` | yes | no: always empty |
| `inject_gpio()` | yes | yes |
| `inject_can()` / `inject_radio()` | yes | no: raises `NotSupported` |
| `parts=` and `networkServices` | yes | no: raises `NotSupported` (a manifest is skipped) |
| Several machines joined by `media` | yes | `run_scenario` and `test.yaml` manifests; `Sim` drives one machine |

"`Sim` only" means a Python test holding a `Sim` can ask for it; a `test.yaml`
manifest has keys for UART text and bus frames and nothing else, on either
engine.

On the Rust engine a scenario with several machines runs in one call, to its
timeout, and what happened is read afterwards. It cannot be paused, so nothing
can be read or injected part-way; `Sim` needs exactly that, and its Rust
session holds a single machine. A manifest runs this way, and so does
`run_scenario`, which also returns the traces:

```python
run = simantic.run_scenario(
    {"machines": {"nodea": {"mcu": "STM32F407VG", "elf": "node.elf"},
                  "nodeb": {"mcu": "STM32F407VG", "elf": "node.elf"}},
     "media": [{"type": "can", "connect": ["nodea.can1", "nodeb.can1"]}]},
    timeout=10,                                  # virtual seconds
    trace_symbols=["can_send"], trace_memory=["rx_count:4"], trace_interrupts=True)

assert "[nodeb] RESULT: PASS" in run.output      # UART text, one "[machine] " prefixed line each
sends = [h for h in run.symbol_trace if h["machine"] == "nodea"]
```

`run.uart_records`, `run.interrupts`, `run.symbol_trace` and `run.memory_trace`
hold the same records the `Sim` methods of those names return, for every
machine, in virtual-time order. Tracing never halts a machine, and a traced
run prints what an untraced one does. A symbol is traced on every machine
whose ELF defines it.

## Timing assertions and the quantum

Renode delivers scheduled events on sync points, so a timing assertion is only
as fine as the scenario's `quantum`. The default (100 µs) is coarser than one
UART character at 115200 (86.8 µs). Set `quantum` in the scenario dict (seconds)
before asserting on intervals — an assertion at the default quantum measures
the time resolution, not the firmware.

## How it runs

`Sim` hosts the engine (`Simantic.Core`, .NET) inside the Python process via
pythonnet and holds a `Session` object — the same `SessionSpec`/`Session` API
the `sim` CLI is built on. There is no subprocess and no protocol: method
calls are method calls, records are objects. The engine is located in this
order: `$SIMANTIC_ENGINE_DIR`; a development `sim` publish directory
(`$SIMANTIC_SIM`); the managed install under `~/.simantic/engine/<version>/`
— and if none exists it is fetched from the public release on the spot.
The managed engine bundles its own .NET runtime, so a machine needs only
Python.

Consequences worth knowing:

- **One emulation per process.** The engine keeps process-global state, so
  run N simulations as N processes (`ProcessPoolExecutor`), never N threads.
- **Scripted peers run in your interpreter.** A `ScriptedNetworkService` or
  `ScriptedCellularPeer` script executes on the emulation thread of the same
  Python process; `expect`/`run_for` release the GIL while the clock runs so
  the peer can take it. Don't hold locks across those calls.
- **Exceptions are engine exceptions.** A bad platform or a missing ELF
  surfaces as `SimError` with the engine's message.

## Why not MCP

MCP is a discovery and permission layer for an interactive agent. A test, or
an agent that needs a loop, is better served by code: the loop runs in the
engine's process, only the conclusion enters the transcript, and the script
becomes a fixture. `Sim` is that path; the MCP servers remain for interactive
poking and are expected to shrink to a thin adapter over it.
