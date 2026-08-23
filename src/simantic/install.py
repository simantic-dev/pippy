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

Fetching requires an account: every request carries the stored token, and
an unauthenticated install stops before it reaches the network.
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

from . import auth

RELEASES_URL = "https://drjdhqfvrttolueolzif.supabase.co/storage/v1/object/public/releases"


def releases_url() -> str:
    """Where manifests are served from. $SIMANTIC_RELEASES_URL overrides.

    Overridable so a release can be rehearsed against a staging host before
    it is published, and so the base can later move behind an endpoint that
    checks the token this client already sends.
    """
    return os.environ.get("SIMANTIC_RELEASES_URL", RELEASES_URL).rstrip("/")


#: Trades the token for a short-lived signed URL into a private bucket.
#:
#: The token check has to happen on the server. This installer is readable —
#: it names its own download URL — so a check performed here is a check any
#: reader can skip by fetching that URL directly. The engine is about a
#: megabyte; re-hosting it is a `curl` and an upload. Only an object that
#: cannot be fetched without a signature makes the check mean anything.
GATE_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/get-release"


def gate_url() -> str:
    """The signing endpoint, or "" to fetch straight from a public bucket.

    Set $SIMANTIC_GATE_URL="" together with $SIMANTIC_RELEASES_URL to install
    from a plain directory of files — used by the tests and by anyone serving
    their own mirror. Unset, the gated path is what runs.
    """
    configured = os.environ.get("SIMANTIC_GATE_URL")
    return (GATE_URL if configured is None else configured).rstrip("/")

#: Binary name -> release product prefix. A product that has published no
#: manifest yet fails with a clear message rather than a stray 404.
PRODUCTS = {
    "sim": "cli",
    "analog-cli": "analog",
    "pyrite": "pyrite",
    "pyrite-mcp": "pyrite",
}

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
    #: Set when the manifest names an object in a gated bucket rather than a
    #: public URL. Signed at download time, because a signature minted when
    #: the manifest was read may have expired by the time the bytes are
    #: wanted.
    path: str | None = None
    channel: str | None = None


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


def _headers(url: str) -> dict[str, str]:
    """Authorization for a release request.

    Raises NotAuthenticated rather than falling back to an anonymous fetch:
    an install must fail closed, and failing here costs nothing but a clear
    message before any network round trip.

    The token is only attached to the release host itself. Artifact URLs come
    out of a manifest, which is data rather than code — a manifest naming
    another host would otherwise have this client hand that host the user's
    credentials. Off-host downloads still happen; they happen anonymously.
    """
    credentials = auth.load()  # fail closed before any request, wherever it goes
    target, home = urllib.parse.urlparse(url), urllib.parse.urlparse(releases_url())
    # Same host, and never in the clear: a bearer token on http is readable by
    # anything on the path, so a staging or local host gets an anonymous fetch
    # rather than the user's credentials.
    if target.netloc != home.netloc or target.scheme != "https":
        return {}
    return {"Authorization": f"Bearer {credentials.api_key}"}


def signed_url(path: str, *, channel: str | None = None, timeout: float = 30) -> str:
    """Ask the gate to sign `path`, proving the token before anything is served.

    A 401 here is the gate doing its job, so it is reported as such rather
    than as a download failure.
    """
    credentials = auth.load()  # fail closed before the request
    endpoint = (
        f"{gate_url()}?path={urllib.parse.quote(path)}"
        f"&channel={urllib.parse.quote(channel or default_channel())}"
    )
    request = urllib.request.Request(
        endpoint, headers={"Authorization": f"Bearer {credentials.api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise auth.NotAuthenticated(
                f"the backend rejected your credentials (HTTP {exc.code}) — "
                "run `smtc auth`"
            ) from None
        if exc.code == 404:
            raise InstallError(f"no release artifact at {path!r}") from None
        raise InstallError(f"release gate failed: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the release gate: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise InstallError(f"release gate returned invalid JSON: {exc}") from None

    url = body.get("url")
    if not isinstance(url, str) or not url:
        raise InstallError("release gate returned no URL")
    return url


def _object_url(path: str, *, channel: str | None = None) -> tuple[str, dict[str, str]]:
    """Where to fetch a release object from, and what to send with it.

    A signed URL carries its own authorisation in the query string, and the
    storage endpoint expects a JWT in an Authorization header — sending the
    PAT alongside it would be rejected. So a signed fetch sends no headers.
    """
    if gate_url():
        return signed_url(path, channel=channel), {}
    url = f"{releases_url()}/{path}"
    return url, _headers(url)


def fetch_manifest(
    binary: str, *, channel: str | None = None, timeout: float = 30
) -> dict:
    product = PRODUCTS.get(binary)
    if product is None:
        raise InstallError(
            f"unknown binary {binary!r}; expected one of {sorted(PRODUCTS)}"
        )
    channel = channel or default_channel()
    url, headers = _object_url(f"{product}/{channel}.json", channel=channel)
    request = urllib.request.Request(url, headers=headers)
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


def resolve(
    binary: str, *, rid: str | None = None, channel: str | None = None
) -> Artifact:
    """The artifact this machine should download."""
    manifest = fetch_manifest(binary, channel=channel)
    version = manifest.get("version")
    artifacts = manifest.get("artifacts")
    if not version or not isinstance(artifacts, dict):
        raise InstallError("release manifest is missing version or artifacts")

    rid = rid or current_rid()
    entry = artifacts.get(rid)
    # A gated manifest names `path` (an object in a private bucket); a public
    # one names `url`. Either is enough to locate the build.
    if not entry or not (entry.get("url") or entry.get("path")):
        available = ", ".join(sorted(artifacts)) or "none"
        raise InstallError(
            f"no {rid} build in {binary} release {version} (available: {available})"
        )
    return Artifact(
        version=version,
        url=entry.get("url", ""),
        sha256=entry.get("sha256"),
        path=entry.get("path"),
        channel=channel or default_channel(),
    )


def download(artifact: Artifact, *, timeout: float = 300) -> bytes:
    """Fetch the artifact and verify its checksum before it is trusted."""
    if artifact.path:
        url, headers = _object_url(artifact.path, channel=artifact.channel)
    else:
        url, headers = artifact.url, _headers(artifact.url)
    request = urllib.request.Request(url, headers=headers)
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

    Two archives from the `sim` release manifest, each under the release
    bucket's 50 MB object limit: `engine-<rid>` (Simantic.Core and friends)
    unpacked to <version>/, and `dotnet-<rid>` (a private .NET runtime)
    unpacked to <version>/dotnet/. Same credentials and gate as the binaries.
    """
    rid = current_rid()
    engine = resolve("sim", rid=f"{ENGINE_KEY}-{rid}", channel=channel)
    target = engine_root() / engine.version
    if (target / "Simantic.Core.dll").exists() and (target / "dotnet" / "host").is_dir() and not force:
        return target
    runtime = resolve("sim", rid=f"dotnet-{rid}", channel=channel)

    incoming = target.with_name(f".{engine.version}.incoming")
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir(parents=True)
    try:
        _unpack(download(engine), incoming, "engine")
        _unpack(download(runtime), incoming / "dotnet", "runtime")
        if target.exists():
            shutil.rmtree(target)
        os.replace(incoming, target)
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(incoming, ignore_errors=True)
        raise InstallError(f"cannot unpack the engine into {target}: {exc}") from None
    return target


def _unpack(payload: bytes, dest: Path, what: str) -> None:
    if not payload.startswith(b"\x1f\x8b"):
        raise InstallError(f"{what} artifact is not a gzipped tar archive")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            # Refuse anything that would land outside the target.
            resolved = (dest / member.name).resolve()
            if not str(resolved).startswith(str(dest.resolve())):
                raise InstallError(f"{what} archive has an unsafe path: {member.name}")
        archive.extractall(dest, filter="data")


def installed_version(binary: str) -> str | None:
    """Nothing is recorded locally, so this reports presence, not version."""
    target = bin_dir() / binary
    return str(target) if target.exists() else None
