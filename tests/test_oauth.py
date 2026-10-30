import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from amirmcp.oauth import CLAUDE_CALLBACK
from amirmcp.server import build_app


def test_oauth_and_existing_paths(tmp_path):
    secret, read_secret = "full-secret-for-testing", "read-secret-for-testing"
    state = str(tmp_path / "oauth.sqlite3")
    app = build_app(secret, read_secret, "https://example.com", state)
    headers = {"accept": "application/json, text/event-stream"}
    tools = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with TestClient(app, base_url="https://example.com") as client:
        full = client.post(f"/{secret}/mcp", json=tools, headers=headers)
        read = client.post(f"/{read_secret}/mcp", json=tools, headers=headers)
        assert full.status_code == read.status_code == 200
        full_names = {t["name"] for t in full.json()["result"]["tools"]}
        read_names = {t["name"] for t in read.json()["result"]["tools"]}
        assert {"send_message", "press_keys"} <= full_names
        assert not {"send_message", "press_keys"} & read_names
        assert client.post("/wrong/mcp", json=tools, headers=headers).status_code == 404
        assert client.post("/oauth/mcp", json=tools, headers=headers).status_code == 401
        assert client.get("/.well-known/oauth-protected-resource/oauth/mcp").status_code == 200
        registration = {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}
        assert client.post("/register", json={**registration, "redirect_uris": ["https://evil.example/callback"]}).status_code == 400
        registered = client.post("/register", json=registration)
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        response = client.get("/authorize", params={"client_id": client_id, "response_type": "code", "redirect_uri": CLAUDE_CALLBACK, "state": "test-state", "code_challenge": challenge, "code_challenge_method": "S256", "resource": "https://example.com/oauth/mcp"}, follow_redirects=False)
        assert response.status_code == 302, response.text
        consent = response.headers["location"]
        request_id = parse_qs(urlparse(consent).query)["request"][0]
        assert client.post("/oauth/consent", data={"request": request_id, "secret": "wrong"}).status_code == 200
        response = client.post("/oauth/consent", data={"request": request_id, "secret": secret}, follow_redirects=False)
        assert response.status_code == 303
        code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier}
        assert client.post("/token", data={**form, "code_verifier": "wrong"}).status_code == 400
        response = client.post("/token", data=form)
        assert response.status_code == 200, response.text
        token = response.json()
        assert client.post("/token", data=form).status_code == 400
        response = client.post("/oauth/mcp", json=tools, headers={**headers, "authorization": "Bearer " + token["access_token"]})
        assert response.status_code == 200, response.text
        assert {t["name"] for t in response.json()["result"]["tools"]} == full_names

    app = build_app(secret, read_secret, "https://example.com", state)
    with TestClient(app, base_url="https://example.com") as client:
        response = client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": token["refresh_token"]})
        assert response.status_code == 200, response.text
        assert client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": token["refresh_token"]}).status_code == 400
        assert client.post("/oauth/mcp", json=tools, headers={**headers, "authorization": "Bearer " + response.json()["access_token"]}).status_code == 200
