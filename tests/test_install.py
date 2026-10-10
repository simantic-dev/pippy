"""Binary installation: manifest handling, checksum enforcement, placement."""

import hashlib
import io
import json
import os
import zipfile

import pytest

from simantic import auth, install
from simantic._locate import BinaryNotFound, locate

MANIFEST = {
    "version": "0.4.0",
    "artifacts": {
        "osx-arm64": {"url": "https://example.invalid/sim.zip", "sha256": "ab" * 32},
        "linux-x64": {"url": "https://example.invalid/sim-linux.zip"},
    },
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SIMANTIC_HOME", raising=False)
    # Fetching requires an account, so the common case here is authenticated.
    auth.save("smtc_" + "a" * 32, "dev@example.com")
    # Most of this file is about what happens to a URL once it is known —
    # channels, products, checksums, placement. The gate that decides *which*
    # URL is exercised in "the release gate" below, and turned off here so
    # those tests read the direct one.
    return tmp_path


def zipped(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, body)
    return buf.getvalue()


# --- the account gate ---


# --- release channels ---


def test_latest_is_the_default_channel(monkeypatch):
    assert _requested_url(monkeypatch, {}) .endswith("/cli/latest.signed.json")


def test_channel_argument_selects_the_manifest(monkeypatch):
    url = _requested_url(monkeypatch, {}, channel="testing")
    assert url.endswith("/cli/testing.signed.json")


def test_environment_selects_the_channel(monkeypatch):
    """So a tester can opt in once instead of on every command."""
    assert _requested_url(monkeypatch, {"SIMANTIC_CHANNEL": "testing"}).endswith(
        "/cli/testing.signed.json"
    )


def test_explicit_channel_beats_the_environment(monkeypatch):
    url = _requested_url(monkeypatch, {"SIMANTIC_CHANNEL": "testing"}, channel="latest")
    assert url.endswith("/cli/latest.signed.json")


def test_releases_host_can_be_redirected(monkeypatch):
    """Rehearse a release against staging before publishing it."""
    url = _requested_url(monkeypatch, {"SIMANTIC_RELEASES_URL": "http://localhost:8765/"})
    assert url == "http://localhost:8765/cli/latest.signed.json"


def _requested_url(monkeypatch, env, *, binary="sim", **kwargs) -> str:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = []

    def capture(request, **k):
        seen.append(request.full_url)
        raise install.urllib.error.URLError("stop here")

    monkeypatch.setattr(install.urllib.request, "urlopen", capture)
    with pytest.raises(install.InstallError):
        install.fetch_manifest(binary, **kwargs)
    return seen[0]


# --- platform mapping ---


@pytest.mark.parametrize(
    "system, machine, expected",
    [
        ("Darwin", "arm64", "osx-arm64"),
        ("Darwin", "x86_64", "osx-x64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Windows", "AMD64", "win-x64"),
    ],
)
def test_rid_matches_the_manifest_keys(system, machine, expected, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: system)
    monkeypatch.setattr(install.platform, "machine", lambda: machine)
    assert install.current_rid() == expected


def test_unsupported_platform_is_refused(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "SunOS")
    monkeypatch.setattr(install.platform, "machine", lambda: "sparc")
    with pytest.raises(install.InstallError, match="unsupported platform"):
        install.current_rid()


# --- manifest resolution ---


def test_resolves_the_artifact_for_this_machine(monkeypatch):
    monkeypatch.setattr(install, "fetch_manifest", lambda b, **k: MANIFEST)
    artifact = install.resolve("sim", rid="osx-arm64")
    assert artifact.version == "0.4.0"
    assert artifact.sha256 == "ab" * 32


def test_missing_rid_lists_what_is_available(monkeypatch):
    monkeypatch.setattr(install, "fetch_manifest", lambda b, **k: MANIFEST)
    with pytest.raises(install.InstallError, match="available: linux-x64, osx-arm64"):
        install.resolve("sim", rid="win-x64")


def test_install_and_status_cover_every_product():
    """`PRODUCTS` names what can be fetched; `_cli.BINARIES` is what the bare
    `simantic install`/`simantic status` cover. A product missing from
    BINARIES silently sits out both.
    """
    from simantic import _cli

    assert {name for name, _ in _cli.BINARIES} == set(install.PRODUCTS)


def test_unknown_binary_is_refused():
    with pytest.raises(install.InstallError, match="unknown binary"):
        install.fetch_manifest("not-a-simulator")


def test_malformed_manifest_is_refused(monkeypatch):
    monkeypatch.setattr(install, "fetch_manifest", lambda b, **k: {"version": "1"})
    with pytest.raises(install.InstallError, match="missing version or artifacts"):
        install.resolve("sim")


# --- download integrity ---


def test_checksum_mismatch_refuses_the_binary(monkeypatch):
    monkeypatch.setattr(
        install.urllib.request, "urlopen", _fake_urlopen(b"tampered")
    )
    artifact = install.Artifact("0.4.0", "https://example.invalid/x", "00" * 32)
    with pytest.raises(install.InstallError, match="checksum mismatch"):
        install.download(artifact)


def test_matching_checksum_is_accepted(monkeypatch):
    payload = b"genuine"
    monkeypatch.setattr(install.urllib.request, "urlopen", _fake_urlopen(payload))
    artifact = install.Artifact(
        "0.4.0", "https://example.invalid/x", hashlib.sha256(payload).hexdigest()
    )
    assert install.download(artifact) == payload


def test_manifest_without_a_checksum_is_refused():
    """The checksum is the only thing that makes the bucket trustworthy, so
    a manifest entry that omits it cannot install anything."""
    manifest = {"version": "0.4.0", "artifacts": {"osx-arm64": {"url": "https://example.invalid/x"}}}
    with pytest.raises(install.InstallError, match="no sha256"):
        install._resolve(manifest, "sim", "osx-arm64")


def test_manifest_http_error_is_an_install_error(monkeypatch):
    def urlopen(*a, **k):
        raise install.urllib.error.HTTPError("u", 404, "nf", {}, None)
    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)
    with pytest.raises(install.InstallError, match="HTTP 404"):
        install.fetch_manifest("sim")


def test_staging_symlink_is_not_followed(monkeypatch, home, tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    install.bin_dir().mkdir(parents=True)
    (install.bin_dir() / ".sim.incoming").symlink_to(victim)
    monkeypatch.setattr(install, "resolve", lambda *a, **k: install.Artifact("0.4.0", "u", "00" * 32))
    monkeypatch.setattr(install, "download", lambda artifact, timeout=300: b"ELF")
    install.install("sim")
    assert victim.read_bytes() == b"keep"
    assert (install.bin_dir() / "sim").read_bytes() == b"ELF"


def _fake_urlopen(payload: bytes):
    class Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda *a, **k: Response()


# --- manifest signatures ---

# A throwaway key that signs nothing real: it exists so these tests can sign.
TEST_N = int(
    "c44280625993bbee6d52677b60a5fc32a0daeea4c763f462f0fe976bc993de4b"
    "a6ec8f745e080f9be96da9f40e5e2ec1e33e9d4b261e1e37167cac37e02ce034"
    "bd77e3ab2e2c3c2e404dce825f7cdfbdb9e87e465b0ac447c9f692604a180f5d"
    "3a3acbc1bd733041794bd0c5018923897304133d4007cec8bee8ce0ad842ffc6"
    "964c7cbbe75bc7a5aaacd5f25a0ff5d42f68ee1f6bfc03005241775e01c2ac7a"
    "4aff1d471162f7977893146551405e3e91509427008f9f9353180a988ec0187e"
    "77d068c90f7f11033881d02083dde711e98dbf96e143764f30e1369fa9bbc583"
    "d7a28473d4d936d960129d8a36ce07fa191412da3afd46837b21fa453a6a9cbb",
    16,
)
TEST_D = int(
    "0d5753f0db93fe5773d9012dd2e115a6bf668288730169707c5f621db2a3399e"
    "3ce7a1ccd0438e0414371f3176f4920b1e0e7894ce2f87f048b80ae0f57d3774"
    "7e58b30244ee3edd0a040000becaf74ea75f958de4cc739149ba5832f176773c"
    "e8236d0c6b7b74114f5487098d542c3540bb4b2f83b5c429c34882111ca85948"
    "a1032ad83215e3c0d8b2d643e3b4633a2ce23110373eedd660837cf912c1f27e"
    "14df96b37f0e1abe52b03f977872b457edf038d59e69913be342dcb06de65dac"
    "5d7a3e6a2d572101cf9aec01a6fd42cae29e704d7ef6766f8cc4b3850dc30cba"
    "5d526a2dc51644cd17a973e962ef05f8046362d29544b6ad5c427f487cd75041",
    16,
)


def signed(manifest: dict | str, *, d: int = TEST_D, n: int = TEST_N) -> bytes:
    """The envelope the release workflow publishes, signed with the test key."""
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    size = (n.bit_length() + 7) // 8
    digest = install._SHA256_PREFIX + hashlib.sha256(text.encode()).digest()
    block = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(block, "big"), d, n).to_bytes(size, "big")
    return json.dumps({"manifest": text, "signature": signature.hex()}).encode()


@pytest.fixture
def test_key(monkeypatch):
    monkeypatch.setattr(install, "RELEASE_KEYS", ((TEST_N, 65537),))


def test_a_signed_manifest_is_accepted(monkeypatch, test_key):
    monkeypatch.setattr(install.urllib.request, "urlopen", _fake_urlopen(signed(MANIFEST)))
    assert install.fetch_manifest("sim") == MANIFEST


def test_any_listed_key_may_sign(monkeypatch):
    """Rotation: a release signed with the second key installs on a client
    that still lists the first."""
    monkeypatch.setattr(install, "RELEASE_KEYS", (install.RELEASE_KEYS[0], (TEST_N, 65537)))
    assert install._verified_manifest(signed(MANIFEST)) == MANIFEST


def test_a_manifest_signed_by_another_key_is_refused():
    """The shipped keys do not include the test key."""
    with pytest.raises(install.InstallError, match="signature is not valid"):
        install._verified_manifest(signed(MANIFEST))


def test_a_tampered_manifest_is_refused(test_key):
    envelope = json.loads(signed(MANIFEST))
    envelope["manifest"] = envelope["manifest"].replace("example.invalid", "evil.invalid")
    with pytest.raises(install.InstallError, match="signature is not valid"):
        install._verified_manifest(json.dumps(envelope).encode())


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(MANIFEST).encode(),  # the unsigned manifest, as published before
        b'{"manifest": "{}", "signature": "zz"}',
        b'{"manifest": "{}", "signature": ""}',
        b'{"manifest": {}, "signature": "00"}',
        b"not json",
    ],
)
def test_an_unsigned_manifest_is_refused(body, test_key):
    with pytest.raises(install.InstallError):
        install._verified_manifest(body)


def test_the_shipped_key_is_the_release_key():
    """Guards the constant against a stray edit: SHA-256 of the modulus."""
    (n, e), = install.RELEASE_KEYS
    assert n.bit_length() == 4096 and e == 65537
    assert hashlib.sha256(n.to_bytes(512, "big")).hexdigest() == "6957c5a276c268211db612c1916bd3ab133f28f0c60046c27391f6251261d5e9"


# --- extraction ---


def test_extracts_the_named_entry():
    assert install._extract(zipped("sim", b"ELF"), "sim") == b"ELF"


def test_extracts_a_lone_entry_under_another_name():
    assert install._extract(zipped("sim-0.4.0", b"ELF"), "sim") == b"ELF"


def test_extracts_from_a_gzipped_tarball():
    """Passing a .tar.gz through would install a tarball."""
    assert install._extract(tarred("sim", b"ELF"), "sim") == b"ELF"


def test_extracts_a_nested_entry():
    """Some archives put the binary under a directory."""
    assert install._extract(tarred("sim-osx-arm64/sim", b"ELF"), "sim") == b"ELF"


def tarred(name: str, body: bytes) -> bytes:
    import io as _io
    import tarfile as _tarfile

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as archive:
        info = _tarfile.TarInfo(name)
        info.size = len(body)
        archive.addfile(info, _io.BytesIO(body))
    return buf.getvalue()


def test_raw_payload_passes_through():
    """Older manifests pointed straight at the executable."""
    assert install._extract(b"\x7fELF raw", "sim") == b"\x7fELF raw"


def test_ambiguous_archive_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("a", b"1")
        archive.writestr("b", b"2")
    with pytest.raises(install.InstallError, match="no 'sim' entry"):
        install._extract(buf.getvalue(), "sim")


# --- placement ---


def test_install_writes_an_executable_and_is_found(monkeypatch, home):
    payload = zipped("sim", b"ELF")
    monkeypatch.setattr(
        install,
        "resolve",
        lambda b, **k: install.Artifact(
            "0.4.0", "https://example.invalid/x", hashlib.sha256(payload).hexdigest()
        ),
    )
    monkeypatch.setattr(install.urllib.request, "urlopen", _fake_urlopen(payload))

    path = install.install("sim")
    assert path.read_bytes() == b"ELF"
    assert os.access(path, os.X_OK)
    # The whole point: the resolver now finds it with no configuration.
    assert locate("sim", "SIMANTIC_SIM") == path


def test_install_is_idempotent_without_force(monkeypatch, home):
    target = install.bin_dir() / "sim"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    def explode(*a, **k):
        raise AssertionError("should not re-download")

    monkeypatch.setattr(install, "resolve", explode)
    assert install.install("sim").read_bytes() == b"existing"


def test_env_var_still_wins_over_a_managed_binary(monkeypatch, home, tmp_path):
    target = install.bin_dir() / "sim"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"managed")
    override = tmp_path / "mine"
    override.touch()
    monkeypatch.setenv("SIMANTIC_SIM", str(override))
    assert locate("sim", "SIMANTIC_SIM") == override


def test_simantic_home_redirects_the_bin_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMANTIC_HOME", str(tmp_path / "elsewhere"))
    assert install.bin_dir() == tmp_path / "elsewhere" / "bin"


def test_not_found_message_points_at_the_installer(home, monkeypatch):
    monkeypatch.setenv("PATH", "")  # a real sim on the dev machine must not leak in
    with pytest.raises(BinaryNotFound, match="simantic install sim"):
        locate("sim", "SIMANTIC_SIM")


# --- the release gate ---
#
# The engine is about a megabyte, so a token checked inside this installer is
# not a gate: the installer names its own download URL, and anyone who reads it
# can fetch that URL directly. These tests pin the behaviour that makes the
# check real — an object is reached through a signature the server mints, and
# only after it has seen a valid token.


def gated(monkeypatch, responses):
    """Run with the gate on, capturing requests and replying from `responses`."""
    monkeypatch.delenv("SIMANTIC_GATE_URL", raising=False)
    seen = []

    class Response:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def read(self):
            return self._body

    def capture(request, **k):
        seen.append(request)
        return Response(responses.pop(0))

    monkeypatch.setattr(install.urllib.request, "urlopen", capture)
    return seen


SIGNED = b'{"url": "https://signed.invalid/m", "expires_in": 300}'


# -- the engine archive ------------------------------------------------------


def engine_zip(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


ENGINE_FILES = {
    "Simantic.Core.dll": b"dll",
    "sim.runtimeconfig.json": b"{}",
    "dotnet/host/fxr/10.0.0/libhostfxr.dylib": b"fxr",
}


def fake_release(monkeypatch, files=ENGINE_FILES, version="0.9.0"):
    seen = []
    monkeypatch.setattr(install, "resolve", lambda binary, rid=None, channel=None: (
        seen.append(rid), install.Artifact(version=version, url="https://releases.example/engine.zip", sha256="00" * 32))[1])
    monkeypatch.setattr(install, "download", lambda artifact, timeout=300: engine_zip(files))
    return seen


def test_install_engine_unpacks_under_a_version_dir(home, monkeypatch):
    fake_release(monkeypatch)
    target = install.install_engine()
    assert target == install.engine_root() / "0.9.0"
    assert (target / "Simantic.Core.dll").read_bytes() == b"dll"
    assert (target / "dotnet" / "host" / "fxr" / "10.0.0" / "libhostfxr.dylib").exists()
    assert install.installed_engine() == target


def test_install_engine_asks_for_the_engine_rid(home, monkeypatch):
    seen = fake_release(monkeypatch)
    install.install_engine()
    assert seen == [f"engine-{install.current_rid()}"]


def test_version_order_ignores_junk_and_prereleases():
    names = ["zzz", "0.6.2-rc1", "0.6.2", "0.10.0", "0.9.1"]
    assert max(names, key=install._version_key) == "0.10.0"
    assert max(["0.6.2-rc1", "0.6.2"], key=install._version_key) == "0.6.2"
    assert max(["zzz", "0.6.2"], key=install._version_key) == "0.6.2"


def test_engine_version_must_be_a_plain_directory_name(home, monkeypatch):
    fake_release(monkeypatch, version="../../lib")
    with pytest.raises(install.InstallError, match="not a valid directory name"):
        install.install_engine()


def test_installed_engine_picks_the_newest_version(home, monkeypatch):
    for v in ("0.9.0", "0.10.0", "0.9.1", "zzz"):
        d = install.engine_root() / v
        d.mkdir(parents=True)
        (d / "Simantic.Core.dll").write_bytes(b"")
        (d / "sim.runtimeconfig.json").write_bytes(b"{}")
    assert install.installed_engine().name == "0.10.0"


def test_engine_archive_paths_must_stay_inside(home, monkeypatch):
    monkeypatch.setattr(install, "resolve", lambda binary, rid=None, channel=None: install.Artifact(
        version="0.9.0", url="u", sha256="00" * 32))
    monkeypatch.setattr(install, "download", lambda artifact, timeout=300: engine_zip({"../escape": b"x"}))
    with pytest.raises(install.InstallError):
        install.install_engine()


def test_engine_dir_explains_when_no_release_is_reachable(home, monkeypatch):
    from simantic import engine

    monkeypatch.delenv("SIMANTIC_SIM", raising=False)
    monkeypatch.delenv("SIMANTIC_ENGINE_DIR", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(install, "install_engine", lambda **kw: (_ for _ in ()).throw(install.InstallError("offline")))
    with pytest.raises(engine.EngineNotFound, match="could not fetch the engine: offline"):
        engine.engine_dir()


# -- the Rust engine ----------------------------------------------------------

RUST_MANIFEST = {
    "version": "0.3.0",
    "artifacts": {
        "engine-rust-osx-arm64": {"url": "https://releases.example/r.whl", "sha256": "00" * 32},
        "engine-rust-linux-x64": {"url": "https://releases.example/r.whl", "sha256": "00" * 32},
    },
}


def test_install_rust_engine_unpacks_the_wheel_under_a_version_dir(home, monkeypatch):
    monkeypatch.setattr(install, "fetch_rust_manifest", lambda channel=None: RUST_MANIFEST)
    monkeypatch.setattr(install, "download", lambda artifact, timeout=300: engine_zip(
        {"simantic_rust.abi3.so": b"\x7fELF", "simantic_rust-0.3.0.dist-info/METADATA": b""}))
    monkeypatch.setattr(install, "current_rid", lambda: "osx-arm64")
    target = install.install_rust_engine()
    assert target == install.rust_engine_root() / "0.3.0"
    assert install.is_rust_engine(target)
    assert install.installed_rust_engine() == target


def test_rust_manifest_is_its_own_product(monkeypatch):
    seen = []

    def capture(request, **k):
        seen.append(request.full_url)
        raise install.urllib.error.URLError("stop here")

    monkeypatch.setattr(install.urllib.request, "urlopen", capture)
    with pytest.raises(install.InstallError):
        install.fetch_rust_manifest()
    assert seen[0].endswith("/pyrite/latest.signed.json")


# -- engine layouts ----------------------------------------------------------
#
# maturin ships the Rust engine as a package, not a bare .so. Recognising only
# the flat shape looked harmless (the import still worked once the directory was
# on sys.path) but meant the managed copy was never found, so every process
# re-downloaded the wheel.


def test_a_maturin_package_layout_is_recognised(tmp_path):
    """What `maturin build` actually produces, verified against a real wheel."""
    pkg = tmp_path / "simantic_rust"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .simantic_rust import *\n")
    (pkg / "simantic_rust.abi3.so").write_bytes(b"\x7fELF")
    (tmp_path / "simantic_rust-0.3.0.dist-info").mkdir()
    assert install.is_rust_engine(tmp_path)


def test_a_flat_extension_is_recognised(tmp_path):
    (tmp_path / "simantic_rust.abi3.so").write_bytes(b"\x7fELF")
    assert install.is_rust_engine(tmp_path)


def test_a_directory_without_the_module_is_not(tmp_path):
    (tmp_path / "simantic_rust-0.3.0.dist-info").mkdir()
    (tmp_path / "simantic_rust").mkdir()          # no __init__.py: not importable
    assert not install.is_rust_engine(tmp_path)
