# Stepping one machine: `simantic.Sim`

`Sim` is the in-process pyrite engine as a live object, for a single machine. It
builds the machine inside your Python process and holds it at reset; every method
either advances virtual time by an exact, requested amount or observes state
without advancing it. Between calls nothing runs, so a script is deterministic
and Python think-time is free.

`Sim` is a debugging and stepping layer. It is not the way to run scenarios or
suites: several machines, scripted peers (`parts:` with scripts), media and
bridges belong to the `sim` binary, which Python reaches through
`simantic.run_scenario(...)` and `test.yaml` pytest items (both run
`sim run --scenario ... --record DIR` and read the bundle). There is no
`backend=` argument; pyrite is the only engine.

## Start

```python
from simantic import Sim

# a model name: ~/.sim_cache, else fetched with your credentials and cached (like `sim --mcu`)
Sim(elf="fw.elf", mcu="STM32F401RE", uart="usart2")
```

`symbols_elf=` attaches a companion ELF that carries debug symbols for a
stripped image, e.g. a PlatformIO/IDF `firmware.elf` alongside a stripped
flash container passed as `elf=`. It only changes what `symbol()` and RTOS
introspection can resolve; the image that actually runs is still `elf=`.

`trace_symbols=[...]`, `trace_memory=[...]` and `trace_interrupts=True` turn
on the non-halting instrumentation; read it back with `symbol_trace()`,
`memory_trace()` and `interrupts()`.

## Drive

| method | advances time | returns |
| --- | --- | --- |
| `expect(pattern, timeout=30)` | until the regex matches (or `timeout` wall seconds) | `Match(text, virtual_seconds)`; raises `ExpectTimeout` (an `AssertionError`) |
| `run_for(seconds)` | exactly `seconds` | elapsed virtual time |
| `send(text)` / `send_bytes(b)` | no — delivered when time next advances | |
| `inject_gpio(peripheral, pin, state)` | no | |
| `inject_can(peripheral, id, data)` / `inject_radio(peripheral, frame)` | no | |
| `inject_adc(peripheral, channel, volts)` | no | applies from the next conversion |
| `attach_i2c(peripheral, address, device)` | no | `device.write(bytes)`, `device.read(n) -> bytes`, optional `device.stop()` |
| `attach_spi(peripheral, device)` | no | `device(mosi) -> miso`, called per byte; replaces the platform's device |

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
| `memory_trace()` | `[{t, machine, watch, kind, address, value}]` (with `trace_memory=[...]`; `value` is `None` for a read). |
| `itm()` | `[{t, machine, port, bytes, text}]`: ITM stimulus-port output (with `itm=True`, Cortex-M). |
| `read_memory(addr_or_symbol, count)` / `read_u32(...)` | bytes / int from the system bus (RAM, flash, peripheral registers) |
| `symbol(name)` | ELF symbol address |
| `threads()` / `heap()` | RTOS thread snapshot / heap report, when recognised |

Every observer takes `machine=` in a scenario; the constructor's `machine=`
and `uart=` are the defaults.

## Scope

`Sim` drives one machine. A peripheral that does not offer a capability raises
`SimError` naming it. For anything else, use the binary:

| need | use |
| --- | --- |
| several machines joined by `media` | `simantic.run_scenario(...)` or a `test.yaml` |
| Python scripted peers (`parts:`) | the same: they run in the `sim` process |
| bus frames, traces, UART records for every machine | `run.uart_records`, `run.interrupts`, `run.frames` from `run_scenario`; trace flags via `args=`, printed to `run.stdout` |
| `--real-time`, `--uart-tcp`, `--record` | the `sim` CLI |

```python
run = simantic.run_scenario(
    {"machines": {"nodea": {"mcu": "STM32F407VG", "elf": "node.elf"},
                  "nodeb": {"mcu": "STM32F407VG", "elf": "node.elf"}},
     "media": [{"type": "can", "connect": ["nodea.can1", "nodeb.can1"]}]},
    timeout=10,                                  # virtual seconds
    args=["--trace-symbol", "can_send", "--trace-memory", "rx_count:4"])

assert "[nodeb] RESULT: PASS" in run.output
```

A scenario runs to its timeout in one call and is read afterwards; it cannot be
paused part-way. Tracing never halts a machine.

## How it runs

`Sim` hosts pyrite in the Python process: no subprocess, no protocol. Method
calls are method calls. A bad platform or a missing ELF surfaces as `SimError`
with the engine's message.

## Why not MCP

MCP is a discovery and permission layer for an interactive agent. A test, or
an agent that needs a loop, is better served by code: the loop runs in the
engine's process, only the conclusion enters the transcript, and the script
becomes a fixture. `Sim` is that path; the MCP servers remain for interactive
poking and are expected to shrink to a thin adapter over it.
