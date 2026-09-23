import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import pytest
from autobuild_json.checklive_adapter import AuthTransport, CheckliveProcessor, guarded_auth, load_checklive
from autobuild_json.errors import FlowError
from autobuild_json.input_parser import parse_accounts
from autobuild_json.oauth import OAuthSessions
from autobuild_json.models import SuccessRecord, TokenSet, AccountIdentity
from tests.fakes import Response

class RawSession:
    def __init__(self, responses):
        self.responses = responses
        self.cookies = {}
        self.requests = []
        self.closed = False
    def request(self, **kwargs):
        self.requests.append(kwargs)
        if self.responses:
            response = self.responses.pop(0)
        else:
            state = parse_qs(urlsplit(kwargs["url"]).query)["state"][0]
            response = Response(status=302, location="http://localhost:1455/auth/callback?code=fake&state="+state)
        kwargs["content_callback"](response.content)
        return response
    def close(self):
        self.closed = True

def sequence(password_payload=None):
    return [Response(), Response({"csrfToken":"fake-csrf"}), Response({"url":"https://auth.openai.com/oauth/authorize?state=login"}), Response(),
        Response({"token":"fake-sentinel"}), Response({"page":{"type":"login_password"}}),
        Response({"token":"fake-sentinel"}), Response(password_payload or {"continue_url":"https://chatgpt.com/"}), Response()]

def test_real_upstream_methods_through_adapter(settings, context):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    raw = RawSession(sequence())
    record = SuccessRecord(id="record", email="u@example.com", account=AccountIdentity(id="account"), tokens=TokenSet(id_token="i",access_token="a",refresh_token="r"))
    processor = CheckliveProcessor(settings, OAuthSessions(settings), SimpleNamespace(complete=lambda grant, transport:record), module=module, raw_factory=lambda:raw)
    result = processor(parse_accounts("u@example.com|private-pass|JBSWY3DPEHPK3PXP").accounts[0], None, context)
    assert result.status == "success"
    assert raw.closed
    assert any(r.get("json") == {"password":"private-pass"} for r in raw.requests)
    assert not any(urlsplit(r["url"]).hostname == "localhost" for r in raw.requests)

@pytest.mark.parametrize("payload,status", [({"page":{"type":"phone_verification"}},"phone_verify"), ({"page":{"type":"email_otp_verification"}},"error")])
def test_detects_phone_and_email(settings, context, payload, status):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    raw = RawSession(sequence(payload))
    processor = CheckliveProcessor(settings, OAuthSessions(settings), None, module=module, raw_factory=lambda:raw)
    result = processor(parse_accounts("u@example.com|p|JBSWY3DPEHPK3PXP").accounts[0], None, context)
    assert result.status == status
    assert raw.closed

@pytest.mark.parametrize("turnstile", [{"dx":"challenge"}, {"required":False,"dx":"challenge"}, {"required":True,"dx":"challenge"}])
def test_sentinel_metadata_preserves_upstream_token_and_continues_to_email(context, monkeypatch, turnstile):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    assert module.sentinel.HAS_SENTINEL_VM is False
    monkeypatch.setattr(module.sentinel, "_generate_requirements_token", lambda *args, **kwargs:"synthetic-p")
    payload = {"token":"synthetic-challenge-token", "turnstile":turnstile}
    upstream_http = SimpleNamespace(post=lambda *args, **kwargs:Response(payload))
    upstream_token = json.loads(module.sentinel.get_sentinel_token(
        upstream_http, device_id="fixture-device", flow="authorize_continue"))
    raw = RawSession([Response(payload), Response({"page":{"type":"login_password"}})])
    transport = AuthTransport(None, context, raw)
    auth = guarded_auth(module, transport)
    auth.device_id = "fixture-device"
    try:
        response = auth.authorize_continue("u@example.com")
        assert response == {"page":{"type":"login_password"}}
        assert len(raw.requests) == 2
        sent = json.loads(raw.requests[1]["headers"]["openai-sentinel-token"])
        assert sent == upstream_token
        assert sent["t"] == ""
        assert raw.requests[1]["url"] == "https://auth.openai.com/api/accounts/authorize/continue"
        assert raw.requests[1]["json"]["username"]["value"] == "u@example.com"
    finally:
        transport.close()


@pytest.mark.parametrize("status,code", [(403,"AUTH_BLOCKED"), (429,"RATE_LIMITED")])
def test_real_sentinel_http_rejection_keeps_precise_stage(settings, context, status, code):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    responses = sequence()
    responses[4] = Response({"error":{"message":"must-not-leak-response"}}, status=status)
    raw = RawSession(responses)
    processor = CheckliveProcessor(settings, OAuthSessions(settings), None, module=module, raw_factory=lambda:raw)
    result = processor(parse_accounts("u@example.com|p|JBSWY3DPEHPK3PXP").accounts[0], None, context)
    assert result.error.code == code
    assert result.error.stage == "sentinel"
    assert len(raw.requests) == 5
    assert "must-not-leak-response" not in str(result.error)
    assert raw.closed


def test_provider_rejects_email_after_sentinel_not_mislabeled_as_metadata_challenge(context):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    raw = RawSession([
        Response({"token":"synthetic-token", "turnstile":{"dx":"challenge"}}),
        Response(status=403),
    ])
    transport = AuthTransport(None, context, raw)
    try:
        with pytest.raises(FlowError) as error:
            guarded_auth(module, transport).authorize_continue("u@example.com")
        assert error.value.code == "AUTH_BLOCKED"
        assert len(raw.requests) == 2
    finally:
        transport.close()


def test_actual_captcha_page_still_stops_before_password(settings, context):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    responses = sequence()
    responses[5] = Response({"page":{"type":"captcha"}})
    raw = RawSession(responses)
    processor = CheckliveProcessor(settings, OAuthSessions(settings), None, module=module, raw_factory=lambda:raw)
    result = processor(parse_accounts("u@example.com|p|JBSWY3DPEHPK3PXP").accounts[0], None, context)
    assert result.error.code == "ACTION_REQUIRED"
    assert result.error.stage == "email"
    assert len(raw.requests) == 6
    assert raw.closed
