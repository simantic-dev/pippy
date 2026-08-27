"""The cloud backend of `Sim`: the engine runs on Simantic's servers, not here.

Same vocabulary as the Renode and Rust backends — this class only translates
each `Sim` call into a request against the Simantic cloud API and back. No
engine binary, no pythonnet, nothing to install: the ELF (and repl/overlay,
when given) are uploaded once to start the session, and every later call is a
short HTTP round trip. `mcu=` resolves server-side, so it also needs no local
`~/.sim_cache`.

    with Sim(elf="fw.elf", repl="board.repl", backend="cloud") as sim:
        sim.expect("ready")

Point at a non-default deployment with $SIMANTIC_CLOUD_URL; auth reuses the
same `~/.sim_id` credentials as the CLIs (`simantic auth`).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import auth
from .mcu import SimError

DEFAULT_BASE_URL = "https://cloud.simantic.dev/api/v1"


class CloudBackend:
    """`Sim`'s network face: one remote session, addressed by `session_id`."""

    def __init__(self, machines: list[dict], *, base: Path, media, services, quantum,
                 trace_symbols, trace_interrupts, engine_dir):
        # engine_dir names a local engine checkout; the cloud backend has no
        # local engine, so it is accepted (for a uniform Sim() call site) and
        # ignored.
        self._base_url = os.environ.get("SIMANTIC_CLOUD_URL", DEFAULT_BASE_URL).rstrip("/")
        try:
            self._credentials = auth.load()
        except auth.NotAuthenticated as exc:
            raise SimError(f"backend='cloud' needs credentials: {exc}") from None

        payload = {
            "machines": [self._machine_payload(m, base) for m in machines],
            "traceSymbols": list(trace_symbols),
            "traceInterrupts": bool(trace_interrupts),
        }
        if media:
            payload["media"] = media
        if services:
            payload["networkServices"] = [self._service_payload(s, base) for s in services]
        if quantum is not None:
            payload["quantum"] = float(quantum)

        resp = self._request("POST", "/sessions", payload)
        self.session_id: str = resp["sessionId"]
        self.machines: list[str] = list(resp["machines"])

    @staticmethod
    def _machine_payload(m: dict, base: Path) -> dict:
        out = {"name": m["name"], "elf": _b64(base / m["elf"])}
        if m.get("repl"):
            out["repl"] = (base / m["repl"]).read_text()
        if m.get("mcu"):
            out["mcu"] = m["mcu"]
        if m.get("overlay"):
            out["overlay"] = (base / m["overlay"]).read_text()
        return out

    @staticmethod
    def _service_payload(svc: dict, base: Path) -> dict:
        out = dict(svc)
        script = base / svc.get("args", "")
        if svc.get("args") and script.exists():
            out["args"] = script.read_text()
        return out

    # -- stimulus -----------------------------------------------------------

    def send(self, data: bytes, uart: str, machine: str | None) -> None:
        self._request("POST", f"/sessions/{self.session_id}/send",
                       {"data": base64.b64encode(data).decode(), "uart": uart, "machine": machine})

    def inject_gpio(self, peripheral: str, pin: int, state: bool, machine: str | None) -> None:
        self._request("POST", f"/sessions/{self.session_id}/inject/gpio",
                       {"peripheral": peripheral, "pin": int(pin), "state": bool(state), "machine": machine})

    def inject_can(self, peripheral: str, can_id: int, data: bytes, extended: bool, remote: bool,
                   fd: bool, brs: bool, machine: str | None) -> None:
        self._request("POST", f"/sessions/{self.session_id}/inject/can", {
            "peripheral": peripheral, "canId": int(can_id), "data": base64.b64encode(data).decode(),
            "extended": extended, "remote": remote, "fd": fd, "brs": brs, "machine": machine,
        })

    def inject_radio(self, peripheral: str, frame: bytes, machine: str | None) -> None:
        self._request("POST", f"/sessions/{self.session_id}/inject/radio",
                       {"peripheral": peripheral, "frame": base64.b64encode(frame).decode(), "machine": machine})

    # -- time -----------------------------------------------------------------

    def run_for(self, seconds: float) -> float:
        # Wall-clock generous: the server enforces its own execution budget,
        # this just needs to outlast a slow simulation over a slow link.
        resp = self._request("POST", f"/sessions/{self.session_id}/run-for",
                              {"seconds": float(seconds)}, timeout=max(60.0, seconds * 4 + 30))
        return resp["virtualSeconds"]

    @property
    def time(self) -> float:
        return self._request("GET", f"/sessions/{self.session_id}/time")["virtualSeconds"]

    def expect(self, pattern: str, uart: str, machine: str | None, timeout: float) -> tuple[bool, str, float]:
        resp = self._request("POST", f"/sessions/{self.session_id}/expect",
                              {"pattern": pattern, "uart": uart, "machine": machine, "timeout": timeout},
                              timeout=timeout + 30)
        return bool(resp["matched"]), resp["text"], resp["virtualSeconds"]

    # -- observation ----------------------------------------------------------

    def records(self, kind: str, cursor: int, limit: int) -> tuple[list[dict], int, bool]:
        resp = self._request("GET", f"/sessions/{self.session_id}/records/{kind}",
                              params={"cursor": cursor, "limit": limit})
        records = resp["records"]
        if kind == "frames":
            for r in records:
                if r.get("data") is not None:
                    r["data"] = base64.b64decode(r["data"])
        return records, resp["next"], bool(resp["truncated"])

    def read_memory(self, address: int, count: int, machine: str | None) -> bytes:
        resp = self._request("POST", f"/sessions/{self.session_id}/memory/read",
                              {"address": int(address), "count": int(count), "machine": machine})
        return base64.b64decode(resp["data"])

    def symbol(self, name: str, machine: str | None) -> int:
        resp = self._request("POST", f"/sessions/{self.session_id}/symbol", {"name": name, "machine": machine})
        return int(resp["address"])

    def threads(self, machine: str | None):
        return self._request("GET", f"/sessions/{self.session_id}/threads", params={"machine": machine})

    def heap(self, machine: str | None):
        return self._request("GET", f"/sessions/{self.session_id}/heap", params={"machine": machine})

    def close(self) -> None:
        if getattr(self, "session_id", None) is None:
            return
        try:
            self._request("DELETE", f"/sessions/{self.session_id}")
        finally:
            self.session_id = None

    # -- transport --------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None, *,
                 params: dict | None = None, timeout: float = 30) -> dict:
        url = self._base_url + path
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query:
                url = f"{url}?{query}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self._credentials.api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()[:500]
            raise SimError(f"cloud backend: {method} {path} returned HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise SimError(f"cloud backend: cannot reach {self._base_url}: {exc.reason}") from None
        except json.JSONDecodeError as exc:
            raise SimError(f"cloud backend: {method} {path} returned invalid JSON: {exc}") from None


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()
