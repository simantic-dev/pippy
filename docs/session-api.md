# Driving the engine: `simantic.Sim`

`Sim` is the Python face of the simulator — the same capabilities as the
`sim` CLI, as a live object. No test framework is involved; pytest, a plain
script, a notebook or a process pool all use it the same way. It starts `sim --control-stdio`,
which builds the machine(s) and holds them at reset; every method either
advances virtual time by an exact, requested amount or observes state without
advancing it. Between calls nothing runs, so a script is deterministic and
Python think-time is free.

It is the same class for a single board and for a multi-machine scenario with
radio/CAN/Ethernet media and scripted network peers — everything `sim
--scenario` accepts, `Sim(scenario=...)` accepts.

## Start

```python
from simantic import Sim

# a platform file you supply
Sim(elf="fw.elf", repl="board.repl", uart="usart2")

# a model name: ~/.sim_cache, else fetched with your credentials and cached (like `sim --mcu`)
Sim(elf="fw.elf", mcu="STM32F401RE", overlay="overlay.repl-frag", uart="usart2")

# a scenario dict — the sim --scenario schema as Python
Sim(scenario={...}, machine="c6", uart="uart0", cwd=fixture_dir)
```

Scenario dicts use the `sim --scenario` schema verbatim: `machines`
(`repl`/`mcu` + `overlay` + `elf`), `media` (BLE/CAN/Ethernet/UART buses),
`networkServices` (scripted peers), `quantum`. Relative paths resolve against
`cwd=` (default: the process cwd).

`trace_symbols=[...]`, `trace_interrupts=True` and `show_logs=True` turn on the
non-halting instrumentation; read it back with `symbol_trace()`, `interrupts()`
and `logs()`.

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
| `read_uart()` / `uart_records()` | text since the last read / `[{t, machine, label, text}]` |
| `frames()` | `[{t, machine, label, protocol, direction, summary, id, data}]` — SPI/I2C/CAN/BLE/Ethernet |
| `logs()` | `[{t, level, source, message}]` — unhandled registers, model warnings |
| `interrupts()` | `[{t, machine, direction, exception, name}]` (with `trace_interrupts=True`) |
| `symbol_trace()` | `[{t, machine, symbol, address, args}]` (with `trace_symbols=[...]`) |
| `read_memory(addr_or_symbol, count)` / `read_u32(...)` | bytes / int from the system bus (RAM, flash, peripheral registers) |
| `symbol(name)` | ELF symbol address |
| `threads()` / `heap()` | RTOS thread snapshot / heap report, when recognised |

See [Debugging what the firmware is doing](#debugging-what-the-firmware-is-doing)
for the shape of `threads()` and `heap()`, and for the observers the Rust
backend adds beyond this table.

Every observer takes `machine=` in a scenario; the constructor's `machine=`
and `uart=` are the defaults.

## Debugging what the firmware is doing

Everything here is read out of guest memory or out of logs the engine already
fills. Nothing halts the machine, nothing perturbs timing, and no firmware
instrumentation is required — so an assertion made here is an assertion about
the run that actually happened.

Two rules run through the whole surface, and they are worth stating once:

- **Layout comes from the image, never from a table in our source.** Struct
  offsets are read from the ELF's own DWARF, so a kernel option that moves a
  member moves it here too. The alternative — a constant probed once against
  one build — reads a neighbouring member on the next build and reports a
  plausible number, which is worse than reporting nothing.
- **What the target does not record is reported as `None`, not approximated.**
  A missing key is a fact about the build; an invented one is a bug you find
  much later.

### `threads()` — the RTOS thread snapshot

```python
{"rtos": "Zephyr",
 "threads": [{"id": ..., "name": "led1", "state": "ready", "priority": 5,
              "core": 0,
              "stack": {"base": ..., "sizeBytes": 512, "peakUsedBytes": 128}}],
 "truncated": False}
```

`None` when no kernel is recognised — a bare-metal image, or one whose symbols
were stripped.

`truncated` is the adapter's own signal, not a constant. Without the kernel's
all-threads list there is no way to see anything but the thread currently
running, and a one-entry list presented as complete is the worst available
answer. On Zephyr the list needs `CONFIG_THREAD_MONITOR`, names need
`CONFIG_THREAD_NAME`, the published offsets need `CONFIG_DEBUG_THREAD_INFO`,
and `stack.peakUsedBytes` needs `CONFIG_INIT_STACKS` (unpainted stacks have no
high-water mark to find, so the key is present and `None`). A stock build has
none of them — `truncated: True` is the common case, not the exotic one.

This is one of the clearest places where a simulator beats a probe: on a
no-MMU MCU every thread shares one address space, so trace hardware has no
architectural context to observe and cannot see threads at all.

### `heap()` — the allocator report

```python
{"allocator": "Zephyr sys_heap", "arenaSizeBytes": 4180,
 "freeBytes": 3820, "usedBytes": 360,
 "minimumFreeBytes": 3532, "peakUsedBytes": 648, "regions": 1}
```

`None` when no allocator is recognised. Two are:

| allocator | recognised by | how the numbers are obtained |
| --- | --- | --- |
| `ESP-IDF heap_caps` | `registered_heaps` | walks the registered-region list and reads `multi_heap`'s own counters |
| `Zephyr sys_heap` | `_system_heap` | walks the chunk chain structurally — needs no Kconfig and costs the target nothing |

The Zephyr walk validates itself: a correct traversal lands *exactly* on the
sentinel the kernel's own accounting loop terminates against. The chunk field
width is a Kconfig predicate that is invisible in the image, so both widths are
tried and only an exact landing is accepted. If neither lands, the heap reads
as unrecognised rather than as a partial sum.

`minimumFreeBytes` and `peakUsedBytes` are `None` together when the allocator
keeps no low-water mark. ESP-IDF maintains one; Zephyr's chunk chain describes
the heap as it is now and records no history, so the peak is refused rather
than back-computed from current state. `largestFreeBlockBytes` and
`fragmentationRatio` — present on the Renode backend — are absent here rather
than guessed.

### Rust-backend extras

No Renode counterpart yet, so these hang off `sim._b` rather than `Sim`:

| method | returns |
| --- | --- |
| `sim._b.switches()` | `[{t, core, task}]` — the context-switch timeline, from a non-halting watch on the kernel's current-thread pointer |
| `sim._b.task_usage(start, end)` | `[{task, seconds, runs, longestRun}]` over a virtual-time window |
| `sim._b.isr_usage(start, end)` | `{"vectors": [{exception, name, seconds, count, longest, maxDepth}], "threadSeconds": ...}` — works bare-metal too, with no kernel attached |

`interrupts()` needs no flag on this backend: the exception hook is always on,
so the log is there whether or not `trace_interrupts` was passed.

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
