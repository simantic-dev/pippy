"""Credentials for the Simantic backend.

Reads and writes `~/.sim_id`, the same `EMAIL=`/`API_KEY=` file the CLIs use.
Deliberately not a second credential store: authenticating here authenticates
the CLIs too, and vice versa.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

VALIDATE_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/validate-token"
LOGIN_START_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/cli-login-start"
LOGIN_POLL_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/cli-login-poll"

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


def _post_json(url: str, body: dict, *, timeout: float) -> dict:
    """POST a JSON body, return the JSON response. Shared by start and poll."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()[:200]
        raise AuthError(f"{url} returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise AuthError(f"cannot reach the simantic backend: {exc.reason}") from None
    except json.JSONDecodeError as exc:
        raise AuthError(f"{url} returned invalid JSON: {exc}") from None


def browser_login(*, open_browser: bool = True, timeout: float = 600) -> Credentials:
    """Authenticate via the device flow: a browser tab, not a pasted token.

    Mirrors `gh auth login` — the code is printed so it is visible even when
    the browser can't be opened (headless, SSH), and the URL is printed
    unconditionally as the fallback for that case.
    """
    start_url = os.environ.get("SIMANTIC_CLI_LOGIN_START_URL", LOGIN_START_URL)
    poll_url = os.environ.get("SIMANTIC_CLI_LOGIN_POLL_URL", LOGIN_POLL_URL)

    start = _post_json(start_url, {}, timeout=10)
    device_code = start.get("device_code")
    user_code = start.get("user_code")
    verify_url = start.get("verify_url")
    interval = start.get("interval", 3)
    if not device_code or not user_code or not verify_url:
        raise AuthError(f"{start_url} returned an incomplete response")

    print(f"First copy your one-time code: {user_code}")
    print(f"Then open this URL in your browser to confirm: {verify_url}")
    if open_browser:
        try:
            webbrowser.open(verify_url)
        except Exception:
            pass  # the URL above is already the fallback

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = _post_json(poll_url, {"device_code": device_code}, timeout=10)
        status = result.get("status")
        if status == "approved":
            token, email = result.get("token"), result.get("email", "")
            if not token:
                raise AuthError(f"{poll_url} approved without a token")
            save(token, email)
            return Credentials(email=email, api_key=token)
        if status == "pending":
            continue
        raise AuthError(f"sign-in {status}")

    raise AuthError("timed out waiting for browser approval")
