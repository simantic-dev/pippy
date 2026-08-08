"""A session against the pyrite MCP server.

The CLI runner is one-shot: arguments in, transcript out, no state between
calls. That suits a pytest item and suits nothing that needs to look around
while firmware is stopped.

This is the other shape. `pyrite-mcp` keeps a machine alive and exposes it
as JSON-RPC tools over stdio, so a caller can break, step, read memory and
registers, and continue — each call structured going in and coming out,
with no argument strings to build or output to scrape.

    with Session() as sim:
        sim.call("simulate", elf="fw.elf", board="stm32f401")
        print(sim.tools())

The process is an implementation detail of the transport; the interface is
the tool surface, which the server owns and this module does not duplicate.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ._locate import locate
from . import telemetry

ENV_VAR = "SIMANTIC_PYRITE_MCP"
BINARY = "pyrite-mcp"

PROTOCOL_VERSION = "2024-11-05"


class SessionError(RuntimeError):
    """The server could not be started, or refused a call."""


class ToolError(SessionError):
    """A tool ran and reported failure. Distinct so a caller can react to a
    failed operation without treating it as a broken session."""


def mcp_binary(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the pyrite-mcp binary, or raise BinaryNotFound."""
    return locate(BINARY, ENV_VAR, explicit)


class Session:
    """A live pyrite engine, addressed by tool name."""

    def __init__(self, binary: str | os.PathLike[str] | None = None) -> None:
        self._next_id = 0
        try:
            self._proc = subprocess.Popen(
                [str(mcp_binary(binary))],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # diagnostics only; stdout is the protocol
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise SessionError(f"cannot start {BINARY}: {exc}") from None
        self._request("initialize", {"protocolVersion": PROTOCOL_VERSION})

    # --- protocol ---

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        if self._proc.poll() is not None:
            raise SessionError(f"{BINARY} exited with {self._proc.returncode}")
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise SessionError(f"{BINARY} closed its input") from None

        # One response per request, in order: the server is single-threaded
        # over stdio, so the next line is this call's answer.
        line = self._proc.stdout.readline()
        if not line:
            raise SessionError(f"{BINARY} closed its output during {method!r}")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SessionError(f"{BINARY} sent invalid JSON: {exc}") from None
        if "error" in response:
            detail = response["error"]
            raise SessionError(f"{method} failed: {detail.get('message', detail)}")
        return response.get("result")

    # --- surface ---

    def tools(self) -> list[str]:
        """Every tool this server exposes, by name."""
        result = self._request("tools/list", {})
        return [t["name"] for t in result.get("tools", [])]

    def call(self, tool: str, **arguments: Any) -> Any:
        """Invoke a tool. Returns its parsed result.

        A tool that reports failure raises ToolError rather than returning a
        payload the caller has to inspect to notice something went wrong.
        """
        telemetry.record(f"mcp.{tool}")
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        payload = _parsed(result)
        # The envelope's isError is not trusted: pyrite-mcp sets it on
        # successful calls too, so it does not distinguish one from the
        # other. The payload's own `error` does, and is what the tools
        # themselves report through. Envelope flag only when there is no
        # payload to ask.
        if isinstance(payload, dict) and "error" in payload:
            if payload["error"] is not None:
                raise ToolError(f"{tool}: {payload['error']}")
            return payload
        if isinstance(result, dict) and result.get("isError") and not isinstance(payload, dict):
            raise ToolError(f"{tool}: {_text_of(result)}")
        return payload

    # --- lifecycle ---

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _text_of(result: dict) -> str:
    """The text blocks of an MCP result, joined."""
    parts = [
        block.get("text", "")
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _parsed(result: Any) -> Any:
    """A tool result as data where it is data, and as text where it is not.

    Tools return their payload as a JSON string inside a text block, so the
    caller would otherwise parse every response by hand.
    """
    if not isinstance(result, dict):
        return result
    text = _text_of(result)
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
