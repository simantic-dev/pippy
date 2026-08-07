"""Credential handling. No network: validate() is exercised via its parser."""

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
