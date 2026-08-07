"""The `simantic` command: authenticate, install binaries, report status.

Thin by design. It exists so `pip install simantic` is followed by two
obvious commands rather than a documentation hunt, not to become a third
CLI alongside the simulators.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import auth, install
from ._locate import BinaryNotFound, locate
from .mcu import BINARY as SIM_BINARY
from .mcu import ENV_VAR as SIM_ENV
from ._locate import BINARY as ANALOG_BINARY
from ._locate import ENV_VAR as ANALOG_ENV

BINARIES = ((ANALOG_BINARY, ANALOG_ENV), (SIM_BINARY, SIM_ENV))


def _auth(args) -> int:
    token = args.token
    if token is None and not sys.stdin.isatty():
        token = sys.stdin.read().strip()
    if token is None:
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
            path = install.install(name, force=args.force)
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
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="simantic", description="Simantic SDK: authentication and binaries."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="store backend credentials in ~/.sim_id")
    p_auth.add_argument(
        "--token",
        help="personal access token; prompted for, or read from stdin, when omitted",
    )
    p_auth.set_defaults(func=_auth)

    p_install = sub.add_parser("install", help="download simulator binaries")
    p_install.add_argument("binary", nargs="*", help="defaults to all known binaries")
    p_install.add_argument(
        "--force", action="store_true", help="re-download even if already present"
    )
    p_install.set_defaults(func=_install)

    sub.add_parser("status", help="show credentials and resolved binaries").set_defaults(
        func=_status
    )

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (auth.AuthError, install.InstallError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
