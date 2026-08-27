# SDK ⇄ MCP parity: what pippy is still missing

Companion to `../../docs/agentic-simulator-sdk-plan.md` ("the SDK is the real
surface; MCP shrinks to an adapter over it"). That doc's own sketch of the
adapter ("~5 tools: simulate, attach, peek/read, run_python") undersells what
`Simantic.Mcp` actually ships — 28 tools today across ELF inspection,
platform validation, one-shot simulation, and a full GDB control plane. The
rule that matters is the one right above it: **"every MCP capability
reachable as an SDK method; nothing SDK-only hidden behind MCP or vice
versa."** This doc tracks that gap against the current tool inventory
(`MCP/src/Simantic.Mcp/Tools/*.cs`), not an assumed-small adapter.

## Status

### Done (this pass)

| MCP tool | SDK equivalent |
|---|---|
| `elf_symbols` | `simantic.elf_symbols(path)` (`_elf.py`) |
| `list_local_platforms` | `simantic.list_platforms(dir, filter=)` (`platforms.py`) |
| `read_platform` | `simantic.read_platform(path)` |
| `validate_platform` | `simantic.validate_platform(path)` — engine-backed (`renode` only), rebuilds process-wide state like the MCP tool does; same "not concurrent with a live session" rule |
| `simulate` / `simulate_mcu` / `simulate_scenario` | `Sim(elf=, repl=\|mcu=, scenario=)` — already had full parity, this predates the plan |
| `authenticate` / `check_auth` | `auth.login()` / `auth.load()`, `simantic auth` / `simantic status` — already existed under different names |
| `list_models` | `simantic.list_models()` (`_replx.py`) — same `list-supported-mcus` endpoint the MCP tool hits |
| server-side execution, no local install | `backend="cloud"` (previous pass) — client contract only, no server yet |

### Gap: static ELF, no engine

| MCP tool | Notes |
|---|---|
| `elf_disassemble` | No SDK equivalent. MCP's version almost certainly goes through a real disassembler (Capstone or similar via .NET); `_elf.py` has no instruction decoder. New dependency or a subprocess (`objdump`) wrapper needed — not started. |

Also: `_elf.symbols()` (and now `elf_symbols`) only reads 32-bit LE ELF class.
No 64-bit support (RV64, Cortex-A64) — a real gap for those targets, not a
design choice; flagged in the docstring now rather than silently wrong.

### Gap: the GDB control plane (`GdbTools.cs` + `GdbControlTools.cs`, 16 tools)

None of this is in the SDK. It's the biggest remaining piece:

- Session lifecycle over GDB: `start_gdb_session`, `start_scenario_gdb_session`,
  `stop_gdb_session`, `list_gdb_sessions` — a Renode GDB stub an *external*
  debugger can also attach to, distinct from `Sim`'s own in-process control.
- Live inspection while attached: `read_uart`, `read_frames`, `read_renode_logs`.
- The actual debugger verbs: `gdb_attach`/`gdb_detach`, `gdb_break`/
  `gdb_break_list`/`gdb_break_delete`, `gdb_continue`, `gdb_step`,
  `gdb_registers`, `gdb_read_memory`/`gdb_write_memory`, `gdb_eval`,
  `gdb_run_until`, `gdb_backtrace`, `gdb_fault_report`.

`Sim` today has `read_memory`/`read_u32` (no `write_memory`), and nothing for
breakpoints, single-stepping, registers, backtraces, or fault decoding — an
agent doing real firmware debugging (not just UART-watch-and-assert) has no
SDK path and must fall back to raw `gdb` + the MI plane, or MCP.

Two real options, not decided here:
1. Add these as `Sim` methods that talk to the engine directly (pythonnet),
   parallel to how `read_memory`/`symbol` already work — keeps everything
   in-process, no GDB wire protocol involved.
2. Add a `GdbSession` class in the SDK that does what the MCP tools do:
   start a Renode GDB stub, then drive it as a client — reusable by MCP
   itself (kills the drift the plan calls out) but is a second protocol path
   the SDK now owns.
(1) fits "SDK = real surface, MCP = adapter" better; (2) is closer to what
external debuggers need (an agent attaching its own `gdb`). Picking between
them wants the actual next-user story (agent doing its own debugging in
Python vs. wanting a socket for `gdb`/`arm-none-eabi-gdb` to attach to) more
than it wants a guess here.

## Where `backend="cloud"` sits in this

The cloud backend (added in the previous pass) is a different axis from this
gap list: it's about *where the engine runs* (server vs. this process), not
*what capabilities are exposed*. Whatever lands from the gap list above
should reach `backend="cloud"` too — same `Sim` interface, same parity rule —
but that's follow-on once the local (`renode`/`rust`) side of each capability
exists to mirror.

## Suggested order

1. `write_memory` on `Sim` — small delta next to existing `read_memory`, real
   value for anyone poking peripheral registers from a script.
2. GDB control plane — the big one. Needs the design call above before
   writing code (in-process methods vs. a `GdbSession` wire client).
3. `elf_disassemble` — needs a disassembler; scope depends on what MCP's own
   implementation uses (reuse the same one if it's not .NET-only).
