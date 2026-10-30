"""OAuth sign-in for Claude, alongside the existing secret-path endpoints."""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizeError, RefreshToken, RegistrationError,
    TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route


CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class OAuthProvider:
    def __init__(self, secret: str, public_url: str, state_path: str):
        self.secret = secret
        self.public_url = public_url.rstrip("/")
        self.resource = self.public_url + "/oauth/mcp"
        self.pending = {}
        self.codes = {}
        path = Path(state_path).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, info TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, info TEXT NOT NULL)")
        self.db.commit()

    async def get_client(self, client_id):
        row = self.db.execute("SELECT info FROM clients WHERE id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client):
        if not client.redirect_uris or any(str(uri) != CLAUDE_CALLBACK for uri in client.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "Only Claude's callback is supported")
        self.db.execute("INSERT INTO clients VALUES (?, ?)", (client.client_id, client.model_dump_json()))
        self.db.commit()

    async def authorize(self, client, params):
        if params.resource and params.resource != self.resource:
            raise AuthorizeError("invalid_target", "Unknown MCP resource")
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        request_id = secrets.token_urlsafe(32)
        self.pending[request_id] = (client.client_id, params, now + 600)
        return self.public_url + "/oauth/consent?" + urlencode({"request": request_id})

    async def consent(self, request: Request):
        form = await request.form() if request.method == "POST" else {}
        request_id = str(form.get("request", "")) if request.method == "POST" else request.query_params.get("request", "")
        pending = self.pending.get(request_id)
        if not pending or pending[2] < time.time():
            return PlainTextResponse("This sign-in has expired. Connect again in Claude.", status_code=400)
        error = ""
        if request.method == "POST":
            if hmac.compare_digest(str(form.get("secret", "")).encode(), self.secret.encode()):
                del self.pending[request_id]
                client_id, params, _ = pending
                code = secrets.token_urlsafe(32)
                self.codes[code] = AuthorizationCode(
                    code=code, scopes=params.scopes or [], expires_at=time.time() + 300,
                    client_id=client_id, code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource,
                )
                return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=303, headers={"Cache-Control": "no-store"})
            error = "<p>That secret did not match. Try again.</p>"
        return HTMLResponse(
            "<!doctype html><title>Connect Claude to Amir MCP</title>"
            "<h1>Connect Claude to Amir MCP</h1>"
            "<p>Enter this machine's Amir MCP secret to allow Claude to read and send messages to your coding sessions.</p>"
            + error + '<form method="post"><input type="hidden" name="request" value="'
            + html.escape(request_id, quote=True) + '"><label>Amir MCP secret <input name="secret" type="password" required autofocus></label> <button type="submit">Connect</button></form>',
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; form-action 'self' https://claude.ai; base-uri 'none'; frame-ancestors 'none'"},
        )

    async def load_authorization_code(self, client, code):
        return self.codes.get(code)

    def _store_token(self, kind, token):
        digest = hashlib.sha256(token.token.encode()).hexdigest()
        self.db.execute("INSERT INTO tokens VALUES (?, ?, ?)", (digest, kind, token.model_dump_json(exclude={"token"})))

    def _load_token(self, kind, token, model):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT info FROM tokens WHERE hash = ? AND kind = ?", (digest, kind)).fetchone()
        if not row:
            return None
        import json
        return model(token=token, **json.loads(row[0]))

    def _delete_token(self, token):
        return self.db.execute("DELETE FROM tokens WHERE hash = ?", (hashlib.sha256(token.encode()).hexdigest(),)).rowcount

    def _issue(self, client_id, scopes):
        access = AccessToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes, expires_at=int(time.time()) + 3600, resource=self.resource)
        refresh = RefreshToken(token=secrets.token_urlsafe(32), client_id=client_id, scopes=scopes, resource=self.resource)
        self._store_token("access", access)
        self._store_token("refresh", refresh)
        self.db.commit()
        return OAuthToken(access_token=access.token, token_type="Bearer", expires_in=3600, refresh_token=refresh.token, scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        return self._load_token("refresh", refresh_token, RefreshToken)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if not self._delete_token(refresh_token.token):
            raise TokenError("invalid_grant", "Refresh token already used")
        return self._issue(client.client_id, scopes)

    async def load_access_token(self, token):
        return self._load_token("access", token, AccessToken)

    async def revoke_token(self, token):
        self._delete_token(token.token)
        self.db.commit()


class BearerApp:
    def __init__(self, app, provider):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        token = await self.provider.load_access_token(authorization[7:]) if authorization.startswith("Bearer ") else None
        if not token or not token.expires_at or token.expires_at <= time.time():
            metadata = self.provider.public_url + "/.well-known/oauth-protected-resource/oauth/mcp"
            response = PlainTextResponse("Authentication required", status_code=401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'})
            return await response(scope, receive, send)
        scope = dict(scope, path=scope["path"][len("/oauth"):], root_path=scope.get("root_path", "") + "/oauth")
        return await self.app(scope, receive, send)


def oauth_routes(secret, public_url, state_path, app):
    provider = OAuthProvider(secret, public_url, state_path)
    issuer, resource = AnyHttpUrl(provider.public_url), AnyHttpUrl(provider.resource)
    return [
        *create_auth_routes(provider, issuer, client_registration_options=ClientRegistrationOptions(enabled=True)),
        *create_protected_resource_routes(resource, [issuer], resource_name="Amir MCP"),
        Route("/oauth/consent", provider.consent, methods=["GET", "POST"]),
        Route("/oauth/mcp", endpoint=BearerApp(app, provider)),
    ]
