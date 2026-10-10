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

Releases are public objects, keyed by version, so fetching needs no account.
Trust comes from two checks: the manifest is signed with a release key whose
public half is below, and every artifact is verified against the checksum the
manifest gives for it. A manifest that is unsigned, or an artifact without a
checksum, is refused.

What is fetched is the signed form of the manifest:

    releases/<product>/<channel>.signed.json
    {"manifest": "<the manifest text>", "signature": "<hex RSA signature>"}
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import platform
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


#: Public halves of the keys allowed to sign a release manifest, as
#: (modulus, exponent). More than one so a key can be rotated: a release signed
#: with any of them is accepted.
RELEASE_KEYS: tuple[tuple[int, int], ...] = (
    (int(
    "9e3afacf572c746e16f9aa8cf37402f9ee413377101c2cabb83c0f2dcd5089bf"
    "a29c3b0f0e98ae067d934267cb6263e074e2027932caded10d615328d857cee2"
    "d1356e09c1d59c64ec50124ef485370ac593eb524b062eec951e2b88b9e1d5ee"
    "63444f2a8949da2c7a4d1d605c9193d25bd380f925afe09b4032513f0ec8d6ca"
    "6a225c5333c3c64fb6bff897fbaa93c9c2d3fba85930c4cf5463eb17b2a388a6"
    "6b2bb1a11e3c73b789475e7978f4e06ec6ead6f1aaa9b3a066c05d2d5a1584c2"
    "0ae0ec4565d1bb44e7807022216f7a3c4a4f9722ab159c352ae744c81d60fe1a"
    "26c8f0065d0e81537f66dc40d3cbf782dbe189a7119d16d6783d56670fbf965a"
    "3df664a12e9651e8c2622aa561bca00bb3d634f777937c63864b5312b3cc4ef8"
    "3d9d90b3d5e1387ac869022d1f26950c0549caf44c3bf3446902bc21957d66c4"
    "133fccd89144bd92ee7f7d3892cfcc0c9a4723c74ab53e97f6aadbb02a6c34ab"
    "ad948bb674f981068cadc464fa0ab0c01d32083cc4020defc6a8817058987d1a"
    "993a1c1c0fd116ff9ad02bb32f2d4ab7e0532b72be11109ae965f72a71241f30"
    "0d5725d01393f881d08a4c76587578b6afd193c0755009f1670f638195309fe0"
    "e79f052f20f44e1c4dc0d35ac6470fc56d2abf30ad906582703ee8ccb4608f56"
    "d8fa90727295e2ac396c6798249270552c1c70b7aa0c9e7bb7f7d1d27173e68b",
        16,
    ), 65537),
)

#: DER prefix of a SHA-256 DigestInfo (RFC 8017, section 9.2).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _signed_by(message: bytes, signature: bytes, modulus: int, exponent: int) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256, the scheme `openssl dgst -sha256 -sign`
    produces. Verification is one modular exponentiation and a comparison
    against the only encoding that is valid, so it needs nothing beyond the
    standard library."""
    size = (modulus.bit_length() + 7) // 8
    value = int.from_bytes(signature, "big")
    if len(signature) != size or value >= modulus:
        return False
    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(pow(value, exponent, modulus).to_bytes(size, "big"), expected)


def _verified_manifest(body: bytes) -> dict:
    """The manifest inside a signed envelope, or InstallError if no release
    key signed it."""
    try:
        envelope = json.loads(body)
        text = envelope["manifest"]
        signature = bytes.fromhex(envelope["signature"])
        if not isinstance(text, str):
            raise TypeError("manifest is not text")
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError(f"release manifest is not a signed manifest: {exc}") from None
    if not any(_signed_by(text.encode(), signature, n, e) for n, e in RELEASE_KEYS):
        raise InstallError(
            "release manifest signature is not valid. Refusing to install."
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"release manifest is not valid JSON: {exc}") from None


def _fetch_manifest(url: str, missing: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise InstallError(f"{missing} (HTTP {exc.code} from {url})") from None
    except urllib.error.URLError as exc:
        raise InstallError(f"cannot reach the release server: {exc.reason}") from None
    return _verified_manifest(body)


@dataclass(frozen=True)
class Artifact:
    version: str
    url: str
    sha256: str


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
    url = f"{releases_url()}/{product}/{channel}.signed.json"
    return _fetch_manifest(url, f"no published releases for {binary!r}", timeout)


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
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or len(sha256.strip()) != 64:
        raise InstallError(
            f"{rid} build in {binary} release {version} has no sha256; refusing to install"
        )
    return Artifact(version=version, url=entry["url"], sha256=sha256.strip().lower())


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

    actual = hashlib.sha256(payload).hexdigest()
    if actual != artifact.sha256:
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
        # O_EXCL after an unlink: a planted symlink at the staging path would
        # otherwise redirect the payload somewhere else before the rename.
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        with os.fdopen(fd, "wb") as handle:
            handle.write(executable)
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
    """Numeric release order. Anything that is not `N.N.N[-pre]` sorts last,
    so a stray directory under the engine root can never win the election."""
    release, _, pre = name.partition("-")
    pieces = release.split(".")
    if not pieces or not all(p.isdigit() for p in pieces):
        return (0,)
    # A pre-release of X sorts below X itself.
    return (1, tuple(int(p) for p in pieces), pre == "", pre)


def _version_dir(root: Path, version: str) -> Path:
    """`root/<version>`, refusing a version that is not a plain directory name."""
    target = root / version
    if target.resolve().parent != root.resolve() or version.startswith("."):
        raise InstallError(f"release version {version!r} is not a valid directory name")
    return target


def install_engine(*, force: bool = False, channel: str | None = None) -> Path:
    """Download the engine for this machine into engine_root()/<version>.

    One zip from the `sim` release manifest, key `engine-<rid>`: the
    Simantic.Core publish directory plus a private .NET runtime under
    `dotnet/`, laid out exactly as published.
    """
    artifact = resolve("sim", rid=f"{ENGINE_KEY}-{current_rid()}", channel=channel)
    target = _version_dir(engine_root(), artifact.version)
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
    """True when `d` holds an importable `simantic_rust`.

    maturin ships the extension as a package: `simantic_rust/__init__.py`
    beside `simantic_rust/simantic_rust.abi3.so`. A bare `simantic_rust.<ext>`
    laid out flat is equally importable. Accept either, because the only thing
    that matters here is whether putting `d` on `sys.path` makes the module
    import; matching one layout silently re-downloaded the engine on every
    process, since the managed copy was never recognised.
    """
    if not d.is_dir():
        return False
    return any(
        p.name.startswith("simantic_rust.")
        or (p.name == "simantic_rust" and (p / "__init__.py").exists())
        for p in d.iterdir()
    )


def installed_rust_engine() -> Path | None:
    root = rust_engine_root()
    if not root.exists():
        return None
    candidates = [d for d in root.iterdir() if is_rust_engine(d)]
    return max(candidates, key=lambda d: _version_key(d.name)) if candidates else None


def fetch_rust_manifest(*, channel: str | None = None, timeout: float = 30) -> dict:
    channel = channel or default_channel()
    url = f"{releases_url()}/{RUST_ENGINE_PRODUCT}/{channel}.signed.json"
    return _fetch_manifest(url, "no published Rust engine", timeout)


def install_rust_engine(*, force: bool = False, channel: str | None = None) -> Path:
    """Download the Rust engine wheel for this machine and unpack it into
    rust_engine_root()/<version>. A wheel is a zip; the module inside is abi3,
    so one wheel per platform serves every supported Python."""
    artifact = _resolve(fetch_rust_manifest(channel=channel), RUST_ENGINE_KEY, f"{RUST_ENGINE_KEY}-{current_rid()}")
    target = _version_dir(rust_engine_root(), artifact.version)
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
