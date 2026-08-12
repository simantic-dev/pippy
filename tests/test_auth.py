"""Credential handling. No network: validate() is exercised via its parser."""

import json
import stat

import pytest

from simantic import auth


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_round_trips_credentials(home):
    auth.save("smtc_abc", "dev@example.com")
    creds = auth.load()
    assert creds.api_key == "smtc_abc"
    assert creds.email == "dev@example.com"


def test_credentials_are_not_world_readable(home):
    """The file holds a bearer token; other users must not be able to read it."""
    path = auth.save("smtc_abc", "dev@example.com")
    mode = path.stat().st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH


def test_save_leaves_no_temp_file_behind(home):
    auth.save("smtc_abc", "dev@example.com")
    assert [p.name for p in home.iterdir()] == [".sim_id"]


def test_uses_the_shared_credentials_file(home):
    """Same path and format the CLIs read, so one login serves both."""
    auth.save("smtc_abc", "dev@example.com")
    assert (home / ".sim_id").read_text() == "EMAIL=dev@example.com\nAPI_KEY=smtc_abc\n"


def test_reads_a_file_written_by_the_cli(home):
    (home / ".sim_id").write_text("EMAIL=x@y.z\nAPI_KEY=smtc_fromcli\n")
    assert auth.load().api_key == "smtc_fromcli"


def test_missing_credentials_say_what_to_run(home):
    with pytest.raises(auth.NotAuthenticated, match="simantic auth"):
        auth.load()


def test_file_without_a_key_is_not_authenticated(home):
    (home / ".sim_id").write_text("EMAIL=x@y.z\n")
    with pytest.raises(auth.NotAuthenticated, match="no API_KEY"):
        auth.load()


@pytest.mark.parametrize(
    "token, expected",
    [
        ("", "no token"),
        ("ghp_wrongprovider", "smtc_"),
        ("smtc_has space", "whitespace or non-ASCII"),
        ("smtc_nbsp ", "whitespace or non-ASCII"),
    ],
)
def test_bad_tokens_are_refused_before_any_round_trip(token, expected):
    with pytest.raises(auth.AuthError, match=expected):
        auth.check_token(token)


def test_a_well_formed_token_passes_the_local_check():
    auth.check_token("smtc_" + "a" * 32)


@pytest.mark.parametrize(
    "body, expected",
    [
        ('{"email": "a@b.c"}', "a@b.c"),
        ('{"user_email": "a@b.c"}', "a@b.c"),
        ('{"ok": true}', ""),
        ("not json", ""),
        ("[]", ""),
    ],
)
def test_email_is_extracted_leniently(body, expected):
    """A validate-token response that carries no email is still a success."""
    assert auth._email_from(body) == expected


# --- browser_login (device flow) ---


def _queued_responses(bodies):
    """A fake urlopen returning each body from `bodies` in turn."""

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    queue = iter(bodies)

    def urlopen(request, **kwargs):
        return Response(next(queue))

    return urlopen


def _no_sleep(monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: True)


def test_browser_login_prints_the_code_and_url_then_saves_on_approval(home, monkeypatch, capsys):
    _no_sleep(monkeypatch)
    start = json.dumps(
        {
            "device_code": "dc-1",
            "user_code": "ABCD1234",
            "verify_url": "https://simantic.dev/cli-login?code=ABCD1234",
            "expires_in": 600,
            "interval": 3,
        }
    ).encode()
    approved = json.dumps(
        {"status": "approved", "token": "smtc_" + "a" * 32, "email": "dev@example.com"}
    ).encode()
    monkeypatch.setattr(
        auth.urllib.request, "urlopen", _queued_responses([start, approved])
    )

    credentials = auth.browser_login()

    assert credentials.api_key == "smtc_" + "a" * 32
    assert credentials.email == "dev@example.com"
    assert auth.load().api_key == credentials.api_key
    out = capsys.readouterr().out
    assert "ABCD1234" in out
    assert "https://simantic.dev/cli-login?code=ABCD1234" in out


def test_browser_login_keeps_polling_while_pending(home, monkeypatch):
    _no_sleep(monkeypatch)
    start = json.dumps(
        {
            "device_code": "dc-1",
            "user_code": "ABCD1234",
            "verify_url": "https://simantic.dev/cli-login?code=ABCD1234",
            "interval": 1,
        }
    ).encode()
    pending = json.dumps({"status": "pending"}).encode()
    approved = json.dumps(
        {"status": "approved", "token": "smtc_" + "b" * 32, "email": "x@y.z"}
    ).encode()
    monkeypatch.setattr(
        auth.urllib.request,
        "urlopen",
        _queued_responses([start, pending, pending, approved]),
    )

    credentials = auth.browser_login()
    assert credentials.api_key == "smtc_" + "b" * 32


@pytest.mark.parametrize("status", ["expired", "denied", "consumed"])
def test_browser_login_raises_on_a_terminal_status(home, monkeypatch, status):
    _no_sleep(monkeypatch)
    start = json.dumps(
        {
            "device_code": "dc-1",
            "user_code": "ABCD1234",
            "verify_url": "https://simantic.dev/cli-login?code=ABCD1234",
            "interval": 1,
        }
    ).encode()
    terminal = json.dumps({"status": status}).encode()
    monkeypatch.setattr(
        auth.urllib.request, "urlopen", _queued_responses([start, terminal])
    )

    with pytest.raises(auth.AuthError, match=status):
        auth.browser_login()


def test_browser_login_times_out_if_never_approved(home, monkeypatch):
    _no_sleep(monkeypatch)
    start = json.dumps(
        {
            "device_code": "dc-1",
            "user_code": "ABCD1234",
            "verify_url": "https://simantic.dev/cli-login?code=ABCD1234",
            "interval": 1,
        }
    ).encode()
    pending = json.dumps({"status": "pending"}).encode()

    def urlopen(request, **kwargs):
        body = start if urlopen.calls == 0 else pending
        urlopen.calls += 1
        return _queued_responses([body])(request)

    urlopen.calls = 0
    monkeypatch.setattr(auth.urllib.request, "urlopen", urlopen)

    times = iter([0, 0.5, 601])  # deadline check, one poll, then past timeout
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(times))

    with pytest.raises(auth.AuthError, match="timed out"):
        auth.browser_login(timeout=600)


def test_browser_login_a_failed_browser_open_does_not_crash(home, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda seconds: None)

    def explode(url):
        raise RuntimeError("no display")

    monkeypatch.setattr(auth.webbrowser, "open", explode)
    start = json.dumps(
        {
            "device_code": "dc-1",
            "user_code": "ABCD1234",
            "verify_url": "https://simantic.dev/cli-login?code=ABCD1234",
            "interval": 1,
        }
    ).encode()
    approved = json.dumps(
        {"status": "approved", "token": "smtc_" + "c" * 32, "email": ""}
    ).encode()
    monkeypatch.setattr(
        auth.urllib.request, "urlopen", _queued_responses([start, approved])
    )

    credentials = auth.browser_login()
    assert credentials.api_key == "smtc_" + "c" * 32
