import json
import time
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from autobuild_json.tokens import TokenService
from autobuild_json.oauth import OAuthSessions
from autobuild_json.errors import FlowError
from tests.fakes import FakeTransport, Response
from tests.test_oauth import callback

@pytest.fixture
def signed_tokens():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    def make(**overrides):
        claims = {"iss":"https://auth.openai.com", "aud":"app_EMoamEEZ73f0CkXaXp7hrann", "iat":int(time.time()), "exp":int(time.time())+3600, "sub":"fake-user", "email":"u@example.com", "email_verified":True, "https://api.openai.com/auth":{"chatgpt_account_id":"fake-account"}}
        claims.update(overrides)
        return {"id_token":jwt.encode(claims, key, algorithm="RS256", headers={"kid":"test-key"}), "access_token":"fake-access", "refresh_token":"fake-refresh"}
    return make, {"keys":[jwk]}

def grant(settings):
    sessions = OAuthSessions(settings)
    item = sessions.create("u@example.com", None)
    return sessions.claim(item.session_id, callback(item))

def test_valid_tokens_export_exact_schema(settings, signed_tokens):
    make, jwks = signed_tokens
    transport = FakeTransport(Response(make()), Response(jwks))
    record = TokenService().complete(grant(settings), transport)
    assert set(record.model_dump()) == {"id", "email", "account", "tokens"}
    assert record.account.id == "fake-account"
    assert record.id != record.account.id
    assert "fake-refresh" not in repr(record)
    assert transport.requests[0][0] == "POST"

@pytest.mark.parametrize("overrides", [{"email":"other@example.com"}, {"aud":"evil"}, {"iss":"https://evil.test"}, {"exp":1}, {"email_verified":False}, {"https://api.openai.com/auth":{}}])
def test_invalid_identity(settings, signed_tokens, overrides):
    make, jwks = signed_tokens
    with pytest.raises(FlowError):
        TokenService().complete(grant(settings), FakeTransport(Response(make(**overrides)), Response(jwks)))

@pytest.mark.parametrize("code", ["NETWORK_ERROR", "REQUEST_TIMEOUT"])
def test_exchange_not_retried(settings, code):
    details = {"curl_code":28, "endpoint":"/oauth/token", "method":"POST", "host":"auth.openai.com"}
    transport = FakeTransport(FlowError(code, details=details))
    with pytest.raises(FlowError) as error:
        TokenService().complete(grant(settings), transport)
    assert error.value.code == "TOKEN_EXCHANGE_UNCERTAIN"
    assert error.value.details == details
    assert len(transport.requests) == 1
