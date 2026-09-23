"""Real API → scheduler → upstream methods → PKCE → signed token → disk export."""
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from autobuild_json.api import create_app
from autobuild_json.checklive_adapter import CheckliveProcessor, load_checklive
from autobuild_json.oauth import OAuthSessions
from autobuild_json.results import RunStore
from autobuild_json.runner import Runner
from autobuild_json.tokens import TokenService
from tests.fakes import Response


@pytest.mark.parametrize("mfa_status,expected", [(200,"success"),(401,"error"),(400,"phone_verify")])
def test_full_auth_totp_token_pipeline(settings, mfa_status, expected):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    claims = {"iss":"https://auth.openai.com", "aud":"app_EMoamEEZ73f0CkXaXp7hrann",
        "iat":int(time.time()), "exp":int(time.time())+300, "sub":"test-user", "email":"u@example.com",
        "email_verified":True, "https://api.openai.com/auth":{"chatgpt_account_id":"test-account"}}
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid":"test-key"})
    calls, closed = [], []
    class Raw:
        cookies = {}
        def request(self, **kwargs):
            url, body = kwargs["url"], kwargs.get("json", {})
            path = urlsplit(url).path
            calls.append((path, body))
            if path == "/api/auth/csrf":
                response = Response({"csrfToken":"fake"})
            elif path == "/api/auth/signin/openai":
                response = Response({"url":"https://auth.openai.com/log-in"})
            elif path == "/backend-api/sentinel/req":
                response = Response({"token":"fake-sentinel"})
            elif path == "/api/accounts/authorize/continue":
                response = Response({"page":{"type":"login_password"}})
            elif path == "/api/accounts/password/verify":
                response = Response({"page":{"type":"mfa_totp"}, "continue_url":"/mfa-challenge/abcdef"})
            elif path == "/api/accounts/mfa/verify":
                payload = {"continue_url":"https://chatgpt.com/"} if mfa_status==200 else {"error":{"code":"phone_verification_required"}} if mfa_status==400 else {}
                response = Response(payload,status=mfa_status)
            elif path == "/oauth/authorize":
                state = parse_qs(urlsplit(url).query)["state"][0]
                response = Response(status=302,location="http://localhost:1455/auth/callback?code=c&state="+state)
            elif path == "/oauth/token":
                response = Response({"id_token":token, "access_token":"test-access", "refresh_token":"test-refresh"})
            elif path == "/.well-known/jwks.json":
                response = Response({"keys":[jwk]})
            else:
                assert path in {"/", "/log-in", "/api/accounts/mfa/issue_challenge"}
                response = Response()
            kwargs["content_callback"](response.content)
            return response
        def close(self):
            closed.append(True)
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    processor = CheckliveProcessor(settings, OAuthSessions(settings), TokenService(), module=module, raw_factory=Raw)
    runner = Runner(RunStore(settings.data_dir / "runs"), processor)
    origin = "http://127.0.0.1:8787"
    with TestClient(create_app(settings, runner=runner), base_url=origin) as client:
        login = client.post("/api/session",headers={"Origin":origin},json={"token":"test-admin-token"})
        client.headers.update({"Origin":origin, "X-CSRF-Token":login.json()["csrf_token"]})
        started = client.post("/api/jobs",json={"accounts_text":"u@example.com|fake-password|JBSWY3DPEHPK3PXP"})
        job = started.json()["id"]
        runner.wait(job,3)
        snapshot = client.get(f"/api/jobs/{job}")
        assert snapshot.json()["counts"][expected] == 1
        assert "test-refresh" not in snapshot.text and "fake-password" not in snapshot.text
        mfa = next(body for path, body in calls if path=="/api/accounts/mfa/verify")
        assert len(mfa["code"]) == 6 and mfa["code"].isdigit()
        assert mfa["id"] == "abcdef"
        if expected=="success":
            result = client.get(f"/api/jobs/{job}/exports/success").json()[0]
            assert result["tokens"]["id_token"] == token
            assert result["account"]["id"] == "test-account"
        else:
            assert not any(path=="/oauth/token" for path, _ in calls)
            kind = "phone_verify" if expected=="phone_verify" else "errors"
            row = client.get(f"/api/jobs/{job}/exports/{kind}").json()[0]
            assert row["code"] == ("PHONE_VERIFY" if expected=="phone_verify" else "MFA_ERROR")
    assert len(closed) == 1
