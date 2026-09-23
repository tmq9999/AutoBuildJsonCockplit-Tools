from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import pytest
from autobuild_json.checklive_adapter import CheckliveProcessor, load_checklive
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

def test_sentinel_required_turnstile_does_not_fallback(settings, context):
    module = load_checklive(Path(".deps/Check-Account-ChatGPT"))
    responses = sequence()
    responses[4] = Response({"token":"t", "turnstile":{"required":True,"dx":"challenge"}})
    raw = RawSession(responses)
    processor = CheckliveProcessor(settings, OAuthSessions(settings), None, module=module, raw_factory=lambda:raw)
    result = processor(parse_accounts("u@example.com|p|JBSWY3DPEHPK3PXP").accounts[0], None, context)
    assert result.error.code == "ACTION_REQUIRED"
    assert len(raw.requests) == 5
