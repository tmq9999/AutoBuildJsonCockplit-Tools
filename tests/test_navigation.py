import pytest
from autobuild_json.challenges import detect_challenge, inspect_response
from autobuild_json.navigation import OAuthNavigator
from autobuild_json.errors import FlowError
from tests.fakes import FakeTransport, Response

def test_phone_profile_not_challenge():
    assert detect_challenge({"phone_number":None}, "https://auth.openai.com/profile") is None
    assert detect_challenge({"page":{"type":"phone_verification"}}, "") == "PHONE_VERIFY"
    assert detect_challenge({"page":{"type":"mfa_totp"}}, "") is None
    assert detect_challenge({}, "https://auth.openai.com/add-phone") == "PHONE_VERIFY"

@pytest.mark.parametrize("status,payload,code", [(401,{},"INVALID_CREDENTIALS"),(403,{},"AUTH_BLOCKED"),(429,{},"RATE_LIMITED"),(400,{"error":{"code":"account_deactivated"}},"ACCOUNT_DEACTIVATED"),(409,{},"INVALID_STATE"),(200,{"page":{"type":"email_otp_verification"}},"EMAIL_OTP_REQUIRED")])
def test_classifies_response(status, payload, code):
    with pytest.raises(FlowError) as err:
        inspect_response(Response(payload, status), "https://auth.openai.com/api/accounts/password/verify")
    assert err.value.code == code

def test_mfa_401_is_not_bad_password():
    with pytest.raises(FlowError) as err:
        inspect_response(Response(status=401), "https://auth.openai.com/api/accounts/mfa/verify")
    assert err.value.code == "MFA_ERROR"

def test_callback_intercept(context):
    transport = FakeTransport(Response(status=302, location="http://localhost:1455/auth/callback?code=c&state=s"))
    result = OAuthNavigator(transport, context).complete("https://auth.openai.com/oauth/authorize")
    assert result.endswith("code=c&state=s")
    assert len(transport.requests) == 1

def test_reject_external_redirect(context):
    transport = FakeTransport(Response(status=302, location="https://evil.test/"))
    with pytest.raises(FlowError):
        OAuthNavigator(transport, context).complete("https://auth.openai.com/oauth/authorize")
    assert len(transport.requests) == 1

def test_account_chooser_and_workspace(context):
    transport = FakeTransport(
        Response(text='session="us_1234567890ABCDEF"'),
        Response({"continue_url":"/consent"}),
        Response(text='{"workspace_id":"11111111-1111-4111-8111-111111111111"}'),
        Response({"continue_url":"http://localhost:1455/auth/callback?code=c&state=s"}),
    )
    result = OAuthNavigator(transport, context).complete("https://auth.openai.com/choose-an-account")
    assert "callback" in result
    assert transport.requests[1][2]["json"] == {"session_id":"us_1234567890ABCDEF"}

def test_multiple_workspaces_require_selection(context):
    transport = FakeTransport(Response(text='{"workspace_id":"11111111-1111-4111-8111-111111111111"} {"workspace_id":"22222222-2222-4222-8222-222222222222"}'))
    with pytest.raises(FlowError) as err:
        OAuthNavigator(transport, context).complete("https://auth.openai.com/consent")
    assert err.value.code == "WORKSPACE_SELECTION_REQUIRED"
