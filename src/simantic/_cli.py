"""The `simantic` command: authenticate, install binaries, report status.

Thin by design. It exists so `pip install simantic` is followed by two
obvious commands rather than a documentation hunt, not to become a third
CLI alongside the simulators.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import auth, install, telemetry
from ._locate import BinaryNotFound, locate
from .mcu import BINARY as SIM_BINARY
from .mcu import ENV_VAR as SIM_ENV
from ._locate import BINARY as ANALOG_BINARY
from ._locate import ENV_VAR as ANALOG_ENV
from .pyrite import BINARY as PYRITE_BINARY
from .pyrite import ENV_VAR as PYRITE_ENV

BINARIES = (
    (ANALOG_BINARY, ANALOG_ENV),
    (SIM_BINARY, SIM_ENV),
    (PYRITE_BINARY, PYRITE_ENV),
)


def _auth(args) -> int:
    token = args.token
    if token is None and not sys.stdin.isatty():
        token = sys.stdin.read().strip()
    if token is not None:
        credentials = auth.login(token)
    elif sys.stdin.isatty() and not args.no_browser:
        credentials = auth.browser_login()
    else:
        # Echo off: argv is visible to `ps`, and so is a shell history entry.
        token = getpass.getpass("Personal access token (smtc_...): ").strip()
        credentials = auth.login(token)

    where = auth.sim_id_path()
    who = f" for {credentials.email}" if credentials.email else ""
    print(f"Credentials saved to {where}{who}")
    return 0


def _install(args) -> int:
    names = args.binary or [name for name, _ in BINARIES]
    failures = 0
    for name in names:
        try:
            path = install.install(name, force=args.force, channel=args.channel)
            print(f"{name}: {path}")
        except install.InstallError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            failures += 1
    # Partial success is still useful — one engine may be published and the
    # other not — so report it without discarding what did install.
    return 1 if failures == len(names) else 0


def _status(args) -> int:
    try:
        credentials = auth.load()
        who = credentials.email or "(no email recorded)"
        print(f"authenticated: {who}")
    except auth.NotAuthenticated as exc:
        print(f"not authenticated: {exc}")

    print(f"binaries in: {install.bin_dir()}")
    for name, env in BINARIES:
        try:
            print(f"  {name}: {locate(name, env)}")
        except BinaryNotFound:
            print(f"  {name}: not found (run `simantic install {name}`)")
    print(telemetry.describe())
    return 0


def main(argv: list[str] | None = None) -> int:
    # prog is left to argparse so usage reflects however it was invoked:
    # `simantic`, the short `smtc`, or `python -m simantic`.
    parser = argparse.ArgumentParser(
        description="Simantic SDK: authentication and binaries."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="store backend credentials in ~/.sim_id")
    p_auth.add_argument(
        "--token",
        help="personal access token; opens a browser sign-in, or reads stdin "
        "when piped, when omitted",
    )
    p_auth.add_argument(
        "--no-browser",
        action="store_true",
        help="prompt for a token instead of opening a browser",
    )
    p_auth.set_defaults(func=_auth)

    p_install = sub.add_parser("install", help="download simulator binaries")
    p_install.add_argument("binary", nargs="*", help="defaults to all known binaries")
    p_install.add_argument(
        "--force", action="store_true", help="re-download even if already present"
    )
    p_install.add_argument(
        "--channel",
        help="release channel to install from (default: latest, or $SIMANTIC_CHANNEL)",
    )
    p_install.set_defaults(func=_install)

    sub.add_parser("status", help="show credentials and resolved binaries").set_defaults(
        func=_status
    )

    args = parser.parse_args(argv)
    telemetry.record(f"cli.{args.command}")
    try:
        result = args.func(args)
        # A CLI invocation is a natural moment to upload: the user is not
        # waiting on a simulation, and the spool is due at most hourly.
        telemetry.flush()
        return result
    except (auth.AuthError, install.InstallError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
