"""Reporting that a run happened, to the account that ran it.

Deliberately narrow. What goes up is the *shape* of a run — how many tests,
how many passed, which runner, how long — and never its content. Paths,
project names, ELF filenames, testplan names, and UART transcripts are the
customer's intellectual property and are the reason a package like this gets
uninstalled; none of them leave the machine.

Sending is best-effort and silent: telemetry that breaks a test run, slows
it down, or prints a warning is worse than no telemetry. Every failure path
here ends in `return`.

Off unless the user is authenticated, and off whenever $SIMANTIC_TELEMETRY=0
or $DO_NOT_TRACK=1.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request

from . import auth

#: Deliberately not `report-usage`. That endpoint feeds the run statistics,
#: which count every row as a simulation and treat a missing exit code as a
#: failure — a call-count report there would corrupt a live metric.
REPORT_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/report-sdk-usage"

#: The server caps a report at 16 KB. Nothing here approaches it, and the
#: cap is enforced locally so an oversized report is dropped rather than
#: rejected with an error nobody sees.
MAX_BYTES = 16 * 1024


def enabled() -> bool:
    """Whether to report at all.

    DO_NOT_TRACK is honoured because it is the cross-tool convention, and a
    user who has set it should not have to learn ours as well.
    """
    if os.environ.get("SIMANTIC_TELEMETRY", "").strip() in {"0", "false", "off", "no"}:
        return False
    if os.environ.get("DO_NOT_TRACK", "").strip() in {"1", "true", "yes"}:
        return False
    return True


def environment() -> dict[str, str]:
    """The environment a run happened in. No hostname, no user, no paths."""
    from . import __version__

    return {
        "cli_version": __version__,
        "client": "simantic-py",
        "python": platform.python_version(),
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
    }


def report(event: str, **fields: object) -> bool:
    """Send one usage report. Returns whether it was sent.

    The return value is for tests; callers ignore it, because there is
    nothing useful for them to do when telemetry fails.
    """
    if not enabled():
        return False
    try:
        credentials = auth.load()
    except auth.AuthError:
        return False  # unauthenticated: nothing to attribute a report to

    payload = {"event": event, **environment(), **fields}
    body = json.dumps(payload).encode()
    if len(body) > MAX_BYTES:
        return False

    request = urllib.request.Request(
        REPORT_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {credentials.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        # Offline, blocked, slow, or refused — all of which are fine.
        return False


# --- the call spool ---
#
# Which calls get made, buffered locally and uploaded on an interval rather
# than per call. A round trip inside `run_tests` would put the network on the
# critical path of a simulation; a line appended to a file does not.

UPLOAD_INTERVAL = 3600  # seconds


def spool_path():
    from .install import simantic_home

    return simantic_home() / "usage.jsonl"


def stamp_path():
    from .install import simantic_home

    return simantic_home() / "usage.last"


def record(call: str) -> None:
    """Note that `call` happened. Never raises, never blocks on the network.

    Only the name of the call — an identifier from this package's own API —
    is written. Its arguments are the caller's data and are not ours.
    """
    if not enabled():
        return
    try:
        path = spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # One short line, opened append-only per write: concurrent pytest
        # workers appending to the same spool must not interleave, and an
        # O_APPEND write below the pipe buffer is atomic on POSIX.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"call": call}) + "\n")
    except OSError:
        return


def due(interval: float = UPLOAD_INTERVAL) -> bool:
    """Whether the spool is old enough to upload."""
    import time

    try:
        return (time.time() - stamp_path().stat().st_mtime) >= interval
    except OSError:
        return True  # never uploaded: the first flush is due


def flush(*, force: bool = False, interval: float = UPLOAD_INTERVAL) -> bool:
    """Upload the spool as counts per call, if it is due. Best-effort.

    The spool is renamed before it is read, so calls recorded while an upload
    is in flight land in a fresh file and are not lost. A failed upload keeps
    the claimed file and folds it into the next attempt, so an offline week
    reports once rather than not at all.
    """
    if not enabled() or not (force or due(interval)):
        return False
    try:
        spool = spool_path()
        claimed = spool.with_suffix(".sending")
        if spool.exists():
            # Fold any previously failed upload in rather than overwrite it.
            if claimed.exists():
                with open(claimed, "a", encoding="utf-8") as dst:
                    dst.write(spool.read_text(encoding="utf-8"))
                spool.unlink(missing_ok=True)
            else:
                os.replace(spool, claimed)
        if not claimed.exists():
            return False
        counts: dict[str, int] = {}
        for line in claimed.read_text(encoding="utf-8").splitlines():
            try:
                name = json.loads(line).get("call")
            except json.JSONDecodeError:
                continue
            if isinstance(name, str):
                counts[name] = counts.get(name, 0) + 1
    except OSError:
        return False

    if not counts:
        _touch(claimed)
        return False
    if report("usage", calls=counts):
        try:
            claimed.unlink(missing_ok=True)
        except OSError:
            pass
        _touch()
        return True
    return False


def _touch(claimed=None) -> None:
    """Mark an upload attempt, so a failure does not retry on every call."""
    try:
        stamp_path().parent.mkdir(parents=True, exist_ok=True)
        stamp_path().write_text("")
        if claimed is not None:
            claimed.unlink(missing_ok=True)
    except OSError:
        pass


def describe() -> str:
    """What this package reports, in the words a user would want to read."""
    if not enabled():
        return "telemetry: disabled"
    fields = ", ".join(sorted(environment()))
    try:
        pending = len(spool_path().read_text(encoding="utf-8").splitlines())
    except OSError:
        pending = 0
    return (
        f"telemetry: enabled when authenticated\n"
        f"  sends: {fields}, plus test counts and which calls were made\n"
        f"  never sends: file paths, project or test names, firmware, output\n"
        f"  buffered at: {spool_path()} ({pending} calls pending, "
        f"uploaded hourly)\n"
        f"  disable with: SIMANTIC_TELEMETRY=0 (or DO_NOT_TRACK=1)"
    )
