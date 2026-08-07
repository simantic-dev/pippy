"""Fetching simulator binaries.

Reads the same release manifest the CLIs self-update from, so a binary
installed here is the same artifact `sim update` would have produced:

    releases/<product>/latest.json
    {"version": "0.4.0",
     "artifacts": {"osx-arm64": {"url": ..., "sha256": ...}, ...}}

Binaries land in ~/.simantic/bin, which the resolver searches. Nothing is
written into site-packages: an installed package may be read-only, and a
binary there would vanish on the next upgrade.

Fetching requires an account: every request carries the stored token, and
an unauthenticated install stops before it reaches the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import auth

RELEASES_URL = "https://drjdhqfvrttolueolzif.supabase.co/storage/v1/object/public/releases"

#: Binary name -> release product prefix. A product that has published no
#: manifest yet fails with a clear message rather than a stray 404.
PRODUCTS = {
    "sim": "cli",
    "analog-cli": "analog",
}


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


def _headers() -> dict[str, str]:
    """Authorization for a release request.

    Raises NotAuthenticated rather than falling back to an anonymous fetch:
    an install must fail closed, and failing here costs nothing but a clear
    message before any network round trip.
    """
    return {"Authorization": f"Bearer {auth.load().api_key}"}


def fetch_manifest(binary: str, *, timeout: float = 30) -> dict:
    product = PRODUCTS.get(binary)
    if product is None:
        raise InstallError(
            f"unknown binary {binary!r}; expected one of {sorted(PRODUCTS)}"
        )
    url = f"{RELEASES_URL}/{product}/latest.json"
    request = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise auth.NotAuthenticated(
                f"the backend rejected your credentials for {binary!r} "
                "(HTTP {}) — run `simantic auth`".format(exc.code)
            ) from None
        raise InstallError(
            f"no published releases for {binary!r} (HTTP {exc.code} from {url}). "
            "Install the binary yourself and point $SIMANTIC_* at it."
        ) from None
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the release server: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise InstallError(f"release manifest is not valid JSON: {exc}") from None


def resolve(binary: str, *, rid: str | None = None) -> Artifact:
    """The artifact this machine should download."""
    manifest = fetch_manifest(binary)
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
    request = urllib.request.Request(artifact.url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise auth.NotAuthenticated(
                f"the backend rejected your credentials (HTTP {exc.code}) — "
                "run `simantic auth`"
            ) from None
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
    """The executable inside a release zip, or the payload if it is raw.

    Older manifests pointed straight at the executable, so a non-zip payload
    is passed through rather than treated as corrupt.
    """
    if not payload.startswith(b"PK\x03\x04"):
        return payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if binary in names:
            return archive.read(binary)
        if len(names) == 1:
            return archive.read(names[0])
        raise InstallError(
            f"release archive has no {binary!r} entry (contains: {', '.join(names)})"
        )


def install(binary: str, *, force: bool = False) -> Path:
    """Download `binary` into the managed bin directory; return its path."""
    target = bin_dir() / binary
    if target.exists() and not force:
        return target

    artifact = resolve(binary)
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


def installed_version(binary: str) -> str | None:
    """Nothing is recorded locally, so this reports presence, not version."""
    target = bin_dir() / binary
    return str(target) if target.exists() else None
