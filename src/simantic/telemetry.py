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

REPORT_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/report-usage"

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


def describe() -> str:
    """What this package reports, in the words a user would want to read."""
    if not enabled():
        return "telemetry: disabled"
    fields = ", ".join(sorted(environment()))
    return (
        f"telemetry: enabled when authenticated\n"
        f"  sends: {fields}, plus test counts and durations\n"
        f"  never sends: file paths, project or test names, firmware, output\n"
        f"  disable with: SIMANTIC_TELEMETRY=0 (or DO_NOT_TRACK=1)"
    )
