from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FULL_SCOPE = "https://www.googleapis.com/auth/drive"


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str | None
    redirect_uris: tuple[str, ...]


def load_oauth_client(client_secret_path: Path) -> OAuthClient:
    if not client_secret_path.exists():
        raise AuthError(f"Missing OAuth client file: {client_secret_path}")
    data = json.loads(client_secret_path.read_text())
    section = data.get("installed") or data.get("web")
    if not section:
        raise AuthError("OAuth client file must contain an 'installed' or 'web' section")
    client_id = section.get("client_id")
    if not client_id:
        raise AuthError("OAuth client file has no client_id")
    return OAuthClient(
        client_id=client_id,
        client_secret=section.get("client_secret"),
        redirect_uris=tuple(section.get("redirect_uris") or ()),
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _OAuthCallback(http.server.BaseHTTPRequestHandler):
    server: "_OAuthServer"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in params.items()}
        body = b"Catsync authorization received. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _OAuthServer(http.server.HTTPServer):
    result: dict[str, str] | None = None


def authenticate(client_secret_path: Path, token_path: Path, full_access: bool = False) -> Path:
    client = load_oauth_client(client_secret_path)
    scope = FULL_SCOPE if full_access else READONLY_SCOPE
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    bind_host, redirect_host, redirect_path = _select_loopback_redirect(client)
    server = _OAuthServer((bind_host, 0), _OAuthCallback)
    port = server.server_address[1]
    redirect_uri = f"http://{redirect_host}:{port}{redirect_path}"
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    webbrowser.open(url)
    deadline = time.time() + 180
    while time.time() < deadline and server.result is None:
        time.sleep(0.1)
    if server.result is None:
        raise AuthError("Timed out waiting for OAuth callback")
    if server.result.get("state") != state:
        raise AuthError("OAuth state mismatch")
    if "error" in server.result:
        raise AuthError(server.result["error"])
    code = server.result.get("code")
    if not code:
        raise AuthError("OAuth callback did not include an authorization code")
    token = exchange_code(client, code, verifier, redirect_uri)
    token["scope_requested"] = scope
    token["created_at"] = int(time.time())
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token, indent=2) + "\n")
    token_path.chmod(0o600)
    return token_path


def exchange_code(client: OAuthClient, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    payload = {
        "client_id": client.client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client.client_secret:
        payload["client_secret"] = client.client_secret
    return _post_form(TOKEN_URL, payload)


def _select_loopback_redirect(client: OAuthClient) -> tuple[str, str, str]:
    for uri in client.redirect_uris:
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
            continue
        redirect_host = parsed.hostname
        bind_host = "127.0.0.1" if redirect_host == "127.0.0.1" else "localhost"
        return bind_host, redirect_host, parsed.path or ""
    return "127.0.0.1", "127.0.0.1", "/oauth2callback"


def refresh_access_token(client_secret_path: Path, token_path: Path) -> dict[str, Any]:
    client = load_oauth_client(client_secret_path)
    token = json.loads(token_path.read_text())
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise AuthError("Token file has no refresh_token; run catsyncd auth again")
    payload = {
        "client_id": client.client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client.client_secret:
        payload["client_secret"] = client.client_secret
    fresh = _post_form(TOKEN_URL, payload)
    token.update(fresh)
    token["created_at"] = int(time.time())
    token_path.write_text(json.dumps(token, indent=2) + "\n")
    token_path.chmod(0o600)
    return token


def valid_access_token(client_secret_path: Path, token_path: Path) -> str:
    if not token_path.exists():
        raise AuthError(f"Missing token file: {token_path}")
    token = json.loads(token_path.read_text())
    created = int(token.get("created_at", 0))
    expires = int(token.get("expires_in", 0))
    if not token.get("access_token") or time.time() > created + max(0, expires - 120):
        token = refresh_access_token(client_secret_path, token_path)
    access_token = token.get("access_token")
    if not access_token:
        raise AuthError("Token refresh did not return an access_token")
    return access_token


def _post_form(url: str, payload: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise AuthError(f"OAuth HTTP {exc.code}: {detail}") from exc
