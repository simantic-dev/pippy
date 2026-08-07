"""Credentials for the Simantic backend.

Reads and writes `~/.sim_id`, the same `EMAIL=`/`API_KEY=` file the CLIs use.
Deliberately not a second credential store: authenticating here authenticates
the CLIs too, and vice versa.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

VALIDATE_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/validate-token"

#: Personal access tokens carry this prefix; anything else is a paste error.
TOKEN_PREFIX = "smtc_"


class AuthError(RuntimeError):
    """The token is malformed, rejected, or could not be stored."""


class NotAuthenticated(AuthError):
    """No credentials on this machine."""


@dataclass(frozen=True)
class Credentials:
    email: str
    api_key: str


def sim_id_path() -> Path:
    """`~/.sim_id`, honouring $HOME so tests can redirect it."""
    home = os.environ.get("HOME")
    if not home:
        raise AuthError("$HOME is not set")
    return Path(home) / ".sim_id"


def load() -> Credentials:
    """Read stored credentials, or raise NotAuthenticated."""
    path = sim_id_path()
    try:
        text = path.read_text()
    except OSError:
        raise NotAuthenticated(
            f"no credentials at {path} — run `simantic auth`"
        ) from None

    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()

    api_key = fields.get("API_KEY")
    if not api_key:
        raise NotAuthenticated(f"{path} has no API_KEY — run `simantic auth`")
    return Credentials(email=fields.get("EMAIL", ""), api_key=api_key)


def check_token(token: str) -> None:
    """Reject tokens that cannot be valid, before spending a round trip.

    The ASCII check is not cosmetic: a token pasted with a non-breaking space
    or newline would otherwise be smuggled into an Authorization header.
    """
    if not token:
        raise AuthError("no token provided")
    if not token.startswith(TOKEN_PREFIX):
        raise AuthError(
            f"not an {TOKEN_PREFIX} token — create one on the dashboard's "
            "/account/api page"
        )
    if not all(0x21 <= ord(c) <= 0x7E for c in token):
        raise AuthError(
            "token contains whitespace or non-ASCII characters — re-copy it "
            "from /account/api"
        )


def validate(token: str, *, timeout: float = 10) -> str:
    """Ask the backend to accept the token; return the account's email."""
    check_token(token)
    request = urllib.request.Request(
        os.environ.get("SIMANTIC_VALIDATE_URL", VALIDATE_URL),
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError(
                "token rejected by the backend — check it on the dashboard's "
                "/account/api page"
            ) from None
        detail = exc.read().decode(errors="replace").strip()[:200]
        raise AuthError(f"validate-token returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise AuthError(f"cannot reach the simantic backend: {exc.reason}") from None

    return _email_from(body)


def _email_from(body: str) -> str:
    """The email in a validate-token response, or "" if it carries none."""
    import json

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict):
        for key in ("email", "user_email"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return ""


def save(token: str, email: str) -> Path:
    """Store credentials 0600, atomically.

    Written to a temp file that is 0600 from birth and then renamed, so there
    is no world-readable window and no truncate-in-place of a file the CLIs
    may be reading concurrently.
    """
    path = sim_id_path()
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, f"EMAIL={email}\nAPI_KEY={token}\n".encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise AuthError(f"cannot write {path}: {exc}") from None
    return path


def login(token: str) -> Credentials:
    """Validate a token and store it. The whole `simantic auth` flow."""
    email = validate(token)
    save(token, email)
    return Credentials(email=email, api_key=token)
