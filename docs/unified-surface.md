# Where the unified surface lands

This package (`pip install simantic`) is already the front door for the
engines: it locates and installs the Renode and Rust binaries, and `Sim`
dispatches between them by `backend=`. Two extensions of that same role are
planned but not yet built. Recording them here so the direction is written
down before the code is.

## The `Sim` dispatch API grows into the CLI's job too

`Sim(backend="renode"|"rust")` already picks an engine per call from Python.
Today nothing on the command line does the equivalent — `simantic`/`smtc`
only wraps `auth`/`install`/`status`, and running a simulation from a shell
means invoking the engine's own binary directly (`sim ...`) with no backend
choice at that layer.

The plan is for `simantic`/`smtc` to grow the same dispatch `Sim` already has,
instead of engines each keeping a separate CLI surface. Concretely: the
`backend=` selection and the `NotSupported`-with-a-named-gap behavior that
`Sim` already implements become the one place that decision is made, called
from both the Python API and the command line, rather than duplicated if a
CLI grows its own copy.

## The MCP install front door

Two MCP servers exist today, one per engine, and neither is reachable from
this package: `pyrite-mcp` (Rust engine, active) and the Renode engine's MCP
server (stale). Per [`session-api.md`](session-api.md#why-not-mcp), the MCP
servers are expected to shrink into thin adapters over `Sim` as that
unification happens.

This package is the natural client-side install point for whichever unified
MCP server results, for the same reason it already installs the engines
instead of asking a user to find them: `install.py` already has the
"locate this pinned binary, fetch it if missing, verify it against a
checksum" logic (`install_engine`, `install_rust_engine`). An MCP server
binary is one more thing of that shape — `simantic install mcp` mirrors
`simantic install engine`, and `simantic mcp` launches what it resolved, the
same relationship `Sim` has to the simulation binaries today.

This package still would not *implement* the MCP server — that stays where
the engine code lives — it would only be the thing a user runs `pip install`
for and then finds the server through, the same boundary that already holds
for the simulation engines.
