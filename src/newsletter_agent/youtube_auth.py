"""Google sign-in for reading the owner's YouTube likes: the one-time authorization of a desktop
OAuth client, which yields a refresh token, and the access token each run trades it for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from newsletter_agent import http

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
EXCHANGE_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
WAIT_SECONDS = 600
POLL_SECONDS = 5
LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class Client:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


def load_client(path: Path) -> Client:
    """The desktop OAuth client in the JSON file Google Cloud offers for download. Raises
    ValueError when the file holds another kind of client.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        raise ValueError(f"{path} isn't a desktop app client; create one of type Desktop app")
    client_id, client_secret = installed.get("client_id"), installed.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise ValueError(f"{path} has no client_id and client_secret")
    return Client(client_id, client_secret)


def access_token(client: Client, refresh_token: str) -> str:
    """Trade the refresh token for an access token that lasts an hour. Raises http.FetchError
    when Google refuses, as it does once the owner removes the agent's access.
    """
    response = http.post_form(
        EXCHANGE_URL,
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    token = _json(response.body).get("access_token")
    if not isinstance(token, str) or not token:
        raise http.FetchError("Google's token answer had no access token")
    return token


def authorize(client: Client, open_browser: Callable[[str], object] = webbrowser.open) -> str:
    """Run Google's sign-in for a desktop app and return the refresh token: open the consent page
    in the browser, wait up to WAIT_SECONDS for Google's answer on a one-time listener on
    127.0.0.1 only, check its state, and trade its code, with PKCE, for the tokens. Raises
    RuntimeError when no valid answer comes back, and http.FetchError when the trade fails.
    """
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    server = _Listener()
    redirect = f"http://{LOOPBACK}:{server.server_port}"
    try:
        open_browser(_consent_url(client, redirect, verifier, state))
        give_up = time.monotonic() + WAIT_SECONDS
        while not server.answer and time.monotonic() < give_up:
            server.handle_request()
    finally:
        server.server_close()
    return _refresh_token(client, server.answer, state, redirect, verifier)


def _consent_url(client: Client, redirect: str, verifier: str, state: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return f"{AUTH_URL}?" + urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )


def _refresh_token(
    client: Client, answer: dict[str, str], state: str, redirect: str, verifier: str
) -> str:
    if not answer:
        raise RuntimeError(f"no answer from Google within {WAIT_SECONDS // 60} minutes")
    if answer.get("state") != state:
        raise RuntimeError("Google's answer didn't carry this sign-in's state")
    if "error" in answer or not answer.get("code"):
        raise RuntimeError(f"Google refused the sign-in: {answer.get('error', 'no code')}")
    response = http.post_form(
        EXCHANGE_URL,
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code": answer["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
    )
    tokens = _json(response.body)
    if tokens.get("scope") != SCOPE:
        raise RuntimeError(f"Google granted {tokens.get('scope')!r}, not {SCOPE}")
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise RuntimeError("Google's answer had no refresh token")
    return refresh


def _json(body: bytes) -> dict[str, Any]:
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


class _Listener(HTTPServer):
    """A one-time listener on 127.0.0.1, on a port the system picks, for Google's answer."""

    def __init__(self) -> None:
        super().__init__((LOOPBACK, 0), _Answer)
        self.timeout = POLL_SECONDS
        self.answer: dict[str, str] = {}


class _Answer(BaseHTTPRequestHandler):
    """Keeps the first query that carries Google's code or error, and tells the browser it can
    close the tab.
    """

    def do_GET(self) -> None:
        query = dict(parse_qsl(urlsplit(self.path).query))
        server = self.server
        wanted = "code" in query or "error" in query
        if isinstance(server, _Listener) and not server.answer and wanted:
            server.answer.update(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"newsletter-agent: Google's answer was received. You can close this tab.")

    def log_message(self, format: str, *args: Any) -> None:
        return
