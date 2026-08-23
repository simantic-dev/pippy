"""Fetching simulator binaries.

Reads the same release manifest the CLIs self-update from, so a binary
installed here is the same artifact `sim update` would have produced:

    releases/<product>/<channel>.json
    {"version": "0.4.0",
     "artifacts": {"osx-arm64": {"url": ..., "sha256": ...}, ...}}

The channel is just the manifest name, so publishing a pre-release means
uploading a second pointer beside latest.json rather than standing up
anything new.

Binaries land in ~/.simantic/bin, which the resolver searches. Nothing is
written into site-packages: an installed package may be read-only, and a
binary there would vanish on the next upgrade.

Releases are public objects, keyed by version, so fetching needs no account;
checksums from the manifest are what make a download trustworthy.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import platform
import stat
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


RELEASES_URL = "https://drjdhqfvrttolueolzif.supabase.co/storage/v1/object/public/releases"


def releases_url() -> str:
    """Where manifests are served from. $SIMANTIC_RELEASES_URL overrides.

    Overridable so a release can be rehearsed against a staging host (or a
    plain directory served over HTTP) before it is published.
    """
    return os.environ.get("SIMANTIC_RELEASES_URL", RELEASES_URL).rstrip("/")


#: The manifest to read. $SIMANTIC_CHANNEL selects a pre-release channel.
DEFAULT_CHANNEL = "latest"


def default_channel() -> str:
    return os.environ.get("SIMANTIC_CHANNEL") or DEFAULT_CHANNEL


class InstallError(RuntimeError):
    """The binary could not be fetched or verified."""


@dataclass(frozen=True)
class Artifact:
    version: str
    url: str
    sha256: str | None


def simantic_home() -> Path:
    """Where managed binaries live. $SIMANTIC_HOME overrides."""
    configured = os.environ.get("SIMANTIC_HOME")
    if configured:
        return Path(configured)
    home = os.environ.get("HOME")
    if not home:
        raise InstallError("$HOME is not set; set $SIMANTIC_HOME instead")
    return Path(home) / ".simantic"


def bin_dir() -> Path:
    return simantic_home() / "bin"


def current_rid() -> str:
    """The release-manifest key for this machine.

    Mirrors the .NET runtime identifiers the release workflow publishes under,
    so both installers read the same manifest keys.
    """
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x64", "amd64": "x64",
        "arm64": "arm64", "aarch64": "arm64",
    }.get(machine)
    system = {"darwin": "osx", "linux": "linux", "windows": "win"}.get(
        platform.system().lower()
    )
    if not arch or not system:
        raise InstallError(
            f"unsupported platform: {platform.system()} {platform.machine()}"
        )
    return f"{system}-{arch}"


#: Binary name -> release product prefix.
PRODUCTS = {
    "sim": "cli",
}


def fetch_manifest(
    binary: str, *, channel: str | None = None, timeout: float = 30
) -> dict:
    product = PRODUCTS.get(binary)
    if product is None:
        raise InstallError(
            f"unknown binary {binary!r}; expected one of {sorted(PRODUCTS)}"
        )
    channel = channel or default_channel()
    request = urllib.request.Request(f"{releases_url()}/{product}/{channel}.json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise InstallError(
            f"no published releases for {binary!r} (HTTP {exc.code} from {url}). "
            "Install the binary yourself and point $SIMANTIC_* at it."
        ) from None
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the release server: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise InstallError(f"release manifest is not valid JSON: {exc}") from None


def resolve(
    binary: str, *, rid: str | None = None, channel: str | None = None
) -> Artifact:
    """The artifact this machine should download."""
    return _resolve(fetch_manifest(binary, channel=channel), binary, rid)


def _resolve(manifest: dict, binary: str, rid: str | None) -> Artifact:
    version = manifest.get("version")
    artifacts = manifest.get("artifacts")
    if not version or not isinstance(artifacts, dict):
        raise InstallError("release manifest is missing version or artifacts")

    rid = rid or current_rid()
    entry = artifacts.get(rid)
    if not entry or not entry.get("url"):
        available = ", ".join(sorted(artifacts)) or "none"
        raise InstallError(
            f"no {rid} build in {binary} release {version} (available: {available})"
        )
    return Artifact(version=version, url=entry["url"], sha256=entry.get("sha256"))


def download(artifact: Artifact, *, timeout: float = 300) -> bytes:
    """Fetch the artifact and verify its checksum before it is trusted."""
    request = urllib.request.Request(artifact.url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise InstallError(f"download failed: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise InstallError(f"download failed: {exc.reason}") from None

    if artifact.sha256:
        actual = hashlib.sha256(payload).hexdigest()
        if actual.lower() != artifact.sha256.strip().lower():
            raise InstallError(
                f"checksum mismatch (expected {artifact.sha256}, got {actual}). "
                "Refusing to install."
            )
    return payload


def _extract(payload: bytes, binary: str) -> bytes:
    """The executable inside a release archive, or the payload if it is raw.

    Both archive formats the release workflows produce are handled: zip and
    gzipped tar. Anything else is passed through, because older manifests
    pointed straight at the executable.
    """
    if payload.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            return _pick(names, binary, archive.read)
    if payload.startswith(b"\x1f\x8b"):
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            names = [m.name for m in archive.getmembers() if m.isfile()]

            def read(name: str) -> bytes:
                handle = archive.extractfile(name)
                if handle is None:  # pragma: no cover - filtered to files above
                    raise InstallError(f"cannot read {name!r} from the archive")
                return handle.read()

            return _pick(names, binary, read)
    return payload


def _pick(names: list[str], binary: str, read) -> bytes:
    """The entry that is the executable, by name or by being the only one."""
    for name in names:
        # Release archives sometimes nest the binary under a directory.
        if name == binary or name.endswith(f"/{binary}"):
            return read(name)
    if len(names) == 1:
        return read(names[0])
    raise InstallError(
        f"release archive has no {binary!r} entry (contains: {', '.join(names)})"
    )


def install(binary: str, *, force: bool = False, channel: str | None = None) -> Path:
    """Download `binary` into the managed bin directory; return its path."""
    target = bin_dir() / binary
    if target.exists() and not force:
        return target

    artifact = resolve(binary, channel=channel)
    executable = _extract(download(artifact), binary)

    target.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the target so the rename is atomic on the same filesystem,
    # and a partial download can never be left looking like a usable binary.
    tmp = target.with_name(f".{binary}.incoming")
    try:
        tmp.write_bytes(executable)
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InstallError(f"cannot write {target}: {exc}") from None
    return target


# -- the engine: Simantic.Core + a private .NET runtime, for in-process use ----

ENGINE_KEY = "engine"


def engine_root() -> Path:
    """Where engine releases live: ~/.simantic/engine/<version>/."""
    return simantic_home() / "engine"


def installed_engine() -> Path | None:
    """The newest installed engine directory, or None."""
    root = engine_root()
    if not root.exists():
        return None
    candidates = [
        d for d in root.iterdir()
        if (d / "Simantic.Core.dll").exists() and (d / "sim.runtimeconfig.json").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: _version_key(d.name))


def _version_key(name: str) -> tuple:
    parts = []
    for piece in name.replace("-", ".").split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def install_engine(*, force: bool = False, channel: str | None = None) -> Path:
    """Download the engine for this machine into engine_root()/<version>.

    One zip from the `sim` release manifest, key `engine-<rid>`: the
    Simantic.Core publish directory plus a private .NET runtime under
    `dotnet/`, laid out exactly as published.
    """
    artifact = resolve("sim", rid=f"{ENGINE_KEY}-{current_rid()}", channel=channel)
    target = engine_root() / artifact.version
    if (target / "Simantic.Core.dll").exists() and not force:
        return target

    payload = download(artifact)
    if not payload.startswith(b"PK\x03\x04"):
        raise InstallError("engine artifact is not a zip archive")
    _unpack_zip(payload, target)
    return target


# -- the Rust engine: the simantic_rust extension module ----------------------

RUST_ENGINE_KEY = "engine-rust"
#: The Rust engine is published under its own product manifest.
RUST_ENGINE_PRODUCT = "pyrite"


def rust_engine_root() -> Path:
    """Where Rust engine releases live: ~/.simantic/engine-rust/<version>/."""
    return simantic_home() / "engine-rust"


def is_rust_engine(d: Path) -> bool:
    return d.is_dir() and any(p.name.startswith("simantic_rust.") for p in d.iterdir())


def installed_rust_engine() -> Path | None:
    root = rust_engine_root()
    if not root.exists():
        return None
    candidates = [d for d in root.iterdir() if is_rust_engine(d)]
    return max(candidates, key=lambda d: _version_key(d.name)) if candidates else None


def fetch_rust_manifest(*, channel: str | None = None, timeout: float = 30) -> dict:
    channel = channel or default_channel()
    url = f"{releases_url()}/{RUST_ENGINE_PRODUCT}/{channel}.json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise InstallError(f"no published Rust engine (HTTP {exc.code} from {url})") from None
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the release server: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise InstallError(f"release manifest is not valid JSON: {exc}") from None


def install_rust_engine(*, force: bool = False, channel: str | None = None) -> Path:
    """Download the Rust engine wheel for this machine and unpack it into
    rust_engine_root()/<version>. A wheel is a zip; the module inside is abi3,
    so one wheel per platform serves every supported Python."""
    artifact = _resolve(fetch_rust_manifest(channel=channel), RUST_ENGINE_KEY, f"{RUST_ENGINE_KEY}-{current_rid()}")
    target = rust_engine_root() / artifact.version
    if is_rust_engine(target) and not force:
        return target
    payload = download(artifact)
    if not payload.startswith(b"PK\x03\x04"):
        raise InstallError("Rust engine artifact is not a wheel")
    _unpack_zip(payload, target)
    return target


def _unpack_zip(payload: bytes, target: Path) -> None:
    incoming = target.with_name(f".{target.name}.incoming")
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                # Refuse anything that would land outside the target.
                dest = (incoming / name).resolve()
                if not str(dest).startswith(str(incoming.resolve())):
                    raise InstallError(f"archive has an unsafe path: {name}")
            archive.extractall(incoming)
        if target.exists():
            shutil.rmtree(target)
        os.replace(incoming, target)
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(incoming, ignore_errors=True)
        raise InstallError(f"cannot unpack into {target}: {exc}") from None


def installed_version(binary: str) -> str | None:
    """Nothing is recorded locally, so this reports presence, not version."""
    target = bin_dir() / binary
    return str(target) if target.exists() else None
