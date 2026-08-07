"""Binary installation: manifest handling, checksum enforcement, placement."""

import hashlib
import io
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
    return tmp_path


def zipped(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, body)
    return buf.getvalue()


# --- the account gate ---


def test_fetching_without_an_account_is_refused(home, monkeypatch):
    """Fails closed, and before any network call — no anonymous fallback."""
    (home / ".sim_id").unlink()

    def explode(*a, **k):
        raise AssertionError("must not reach the network unauthenticated")

    monkeypatch.setattr(install.urllib.request, "urlopen", explode)
    with pytest.raises(auth.NotAuthenticated, match="simantic auth"):
        install.install("sim")


def test_the_token_is_sent_with_every_release_request(monkeypatch):
    seen = []

    def capture(request, **k):
        seen.append(request.get_header("Authorization"))
        raise install.urllib.error.URLError("stop here")

    monkeypatch.setattr(install.urllib.request, "urlopen", capture)
    with pytest.raises(install.InstallError):
        install.fetch_manifest("sim")
    with pytest.raises(install.InstallError):
        install.download(install.Artifact("0.4.0", "https://example.invalid/x", None))
    assert seen == ["Bearer smtc_" + "a" * 32] * 2


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_token_says_to_authenticate(code, monkeypatch):
    def refuse(*a, **k):
        raise install.urllib.error.HTTPError("u", code, "no", {}, None)

    monkeypatch.setattr(install.urllib.request, "urlopen", refuse)
    with pytest.raises(auth.NotAuthenticated, match="simantic auth"):
        install.fetch_manifest("sim")


# --- release channels ---


def test_latest_is_the_default_channel(monkeypatch):
    assert _requested_url(monkeypatch, {}) .endswith("/cli/latest.json")


def test_channel_argument_selects_the_manifest(monkeypatch):
    url = _requested_url(monkeypatch, {}, channel="testing")
    assert url.endswith("/cli/testing.json")


def test_environment_selects_the_channel(monkeypatch):
    """So a tester can opt in once instead of on every command."""
    assert _requested_url(monkeypatch, {"SIMANTIC_CHANNEL": "testing"}).endswith(
        "/cli/testing.json"
    )


def test_explicit_channel_beats_the_environment(monkeypatch):
    url = _requested_url(monkeypatch, {"SIMANTIC_CHANNEL": "testing"}, channel="latest")
    assert url.endswith("/cli/latest.json")


def _requested_url(monkeypatch, env, **kwargs) -> str:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = []

    def capture(request, **k):
        seen.append(request.full_url)
        raise install.urllib.error.URLError("stop here")

    monkeypatch.setattr(install.urllib.request, "urlopen", capture)
    with pytest.raises(install.InstallError):
        install.fetch_manifest("sim", **kwargs)
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


def test_manifest_without_a_checksum_still_installs(monkeypatch):
    """Not every published artifact carries one; absence must not block."""
    monkeypatch.setattr(install.urllib.request, "urlopen", _fake_urlopen(b"x"))
    artifact = install.Artifact("0.4.0", "https://example.invalid/x", None)
    assert install.download(artifact) == b"x"


def _fake_urlopen(payload: bytes):
    class Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda *a, **k: Response()


# --- extraction ---


def test_extracts_the_named_entry():
    assert install._extract(zipped("sim", b"ELF"), "sim") == b"ELF"


def test_extracts_a_lone_entry_under_another_name():
    assert install._extract(zipped("sim-0.4.0", b"ELF"), "sim") == b"ELF"


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
    monkeypatch.setattr(
        install,
        "resolve",
        lambda b, **k: install.Artifact("0.4.0", "https://example.invalid/x", None),
    )
    monkeypatch.setattr(install.urllib.request, "urlopen", _fake_urlopen(zipped("sim", b"ELF")))

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
