"""The `simantic` command: authenticate, install binaries, report status,
and build bootable ESP32 images.

Thin by design. It exists so `pip install simantic` is followed by two
obvious commands rather than a documentation hunt, not to become a third
CLI alongside the simulators.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import auth, esp_image, install, telemetry
from ._locate import BinaryNotFound, locate
from .mcu import BINARY as SIM_BINARY
from .mcu import ENV_VAR as SIM_ENV

BINARIES = ((SIM_BINARY, SIM_ENV),)


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
    names = args.binary or [name for name, _ in BINARIES] + [install.ENGINE_KEY, install.RUST_ENGINE_KEY]
    failures = 0
    for name in names:
        try:
            if name == install.ENGINE_KEY:
                path = install.install_engine(force=args.force, channel=args.channel)
            elif name == install.RUST_ENGINE_KEY:
                path = install.install_rust_engine(force=args.force, channel=args.channel)
            else:
                path = install.install(name, force=args.force, channel=args.channel)
            print(f"{name}: {path}")
        except install.InstallError as exc:
            print(f"{name}: {exc}", file=sys.stderr)
            failures += 1
    # Partial success is still useful, so report it without discarding what
    # did install.
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
    engine = install.installed_engine()
    print(f"  engine: {engine if engine else 'not found (fetched on first use, or `simantic install engine`)'}")
    rust = install.installed_rust_engine()
    missing = 'not found (fetched on first use of backend="rust", or `simantic install engine-rust`)'
    print(f"  engine-rust: {rust if rust else missing}")
    print(telemetry.describe())
    return 0


def _parse_part(text: str) -> tuple[int, bytes]:
    offset, sep, path = text.partition(":")
    if not sep or not path:
        raise esp_image.EspImageError(f"--part expects OFFSET:PATH, e.g. 0x10000:firmware.bin (got {text!r})")
    try:
        return int(offset, 0), open(path, "rb").read()
    except ValueError:
        raise esp_image.EspImageError(f"--part offset {offset!r} is not a number") from None
    except OSError as exc:
        raise esp_image.EspImageError(f"--part {path}: {exc.strerror}") from None


def _esp_image(args) -> int:
    size = esp_image.parse_size(args.flash_size) if args.flash_size else None
    if args.flash:
        flash = esp_image._read(args.flash, "flash image")
        if size is not None:
            flash = esp_image.merge_flash([(0, flash)], size=size)
    else:
        flash = esp_image.merge_flash([_parse_part(p) for p in args.part], size=size)
    rom = args.rom
    if rom is None:
        lay = esp_image.layout(args.chip)
        print(f"using Espressif's {lay.chip} mask ROM from {lay.source.url} (cached after the first download)")
    elf = esp_image.build_image(args.chip, flash, out=args.out, rom=rom)
    print(f"wrote {args.out}: {len(elf)} bytes, flash {len(flash):#x} bytes; run it with `sim --elf {args.out}`")
    return 0


def _esp_rom(args) -> int:
    print(esp_image.fetch_rom(args.chip, force=args.force))
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
    p_install.add_argument("binary", nargs="*", help="defaults to all known binaries and both engines")
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

    p_img = sub.add_parser(
        "esp-image",
        help="build a bootable ESP32 image ELF (mask ROM + flash) for `sim --elf`",
    )
    p_img.add_argument("--chip", required=True, help=f"one of {', '.join(esp_image.chips())}")
    src = p_img.add_mutually_exclusive_group(required=True)
    src.add_argument("--flash", help="merged flash image (bootloader, partition table and app at their offsets)")
    src.add_argument(
        "--part",
        action="append",
        metavar="OFFSET:PATH",
        help="a flash part at its offset, repeatable, e.g. 0x0:bootloader.bin 0x8000:partitions.bin "
        "0x10000:firmware.bin (gaps are 0xFF)",
    )
    p_img.add_argument("--flash-size", help="pad the flash image to this size, e.g. 16MB")
    p_img.add_argument(
        "--rom",
        help="use this mask ROM instead of downloading Espressif's pinned copy "
        "(raw dump for C3/C6; esp32p4_rev0_rom.elf for P4)",
    )
    p_img.add_argument("-o", "--out", required=True, help="output ELF path")
    p_img.set_defaults(func=_esp_image)

    p_rom = sub.add_parser("esp-rom", help="download Espressif's pinned mask ROM for a chip and print its path")
    p_rom.add_argument("--chip", required=True, help=f"one of {', '.join(esp_image.chips())}")
    p_rom.add_argument("--force", action="store_true", help="download again even if cached")
    p_rom.set_defaults(func=_esp_rom)

    args = parser.parse_args(argv)
    telemetry.record(f"cli.{args.command}")
    try:
        result = args.func(args)
        # A CLI invocation is a natural moment to upload: the user is not
        # waiting on a simulation, and the spool is due at most hourly.
        telemetry.flush()
        return result
    except (auth.AuthError, install.InstallError, esp_image.EspImageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
