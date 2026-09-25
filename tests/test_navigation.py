import pytest
import base64
import json
from urllib.parse import quote
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


@pytest.mark.parametrize("error", [{"code":"phone_verification"}, {"code":"phone_verification_required"}, "phone_verification_required"])
def test_error_only_phone_challenges(error):
    with pytest.raises(FlowError) as result:
        inspect_response(Response({"error":error}, status=400), "https://auth.openai.com/api/accounts/password/verify")
    assert result.value.code == "PHONE_VERIFY"


def test_phone_profile_error_remains_negative():
    assert detect_challenge({"error":{"code":"profile_update", "phone_number":None}}, "") is None


def test_selected_workspace_in_cookie_precedes_others(context):
    first, second = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    transport = FakeTransport(Response(text=json.dumps({"workspace_id":second})),
        Response({"continue_url":"http://localhost:1455/auth/callback?code=c&state=s"}))
    payload = {"workspace_id":first, "workspaces":[{"id":first},{"id":second}]}
    transport.cookies = {"oai-client-auth-session":base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")}
    OAuthNavigator(transport,context).complete("https://auth.openai.com/consent")
    assert transport.requests[1][2]["json"] == {"workspace_id":first}


def test_urlencoded_json_cookie_workspace(context):
    workspace = "11111111-1111-4111-8111-111111111111"
    transport = FakeTransport(Response(text="<html></html>"),Response({"continue_url":"http://localhost:1455/auth/callback?code=c&state=s"}))
    transport.cookies = {"oai-client-auth-session":quote(json.dumps({"workspaces":[{"id":workspace}]}))}
    OAuthNavigator(transport,context).complete("https://auth.openai.com/consent")
    assert transport.requests[1][2]["json"] == {"workspace_id":workspace}


@pytest.mark.parametrize("path,source", [("/consent","workspace"),("/choose-an-account","account")])
def test_missing_selector_is_parse_error_not_multiple_workspaces(context,path,source):
    transport = FakeTransport(Response(text="<html>Unrecognized UI</html>"))
    with pytest.raises(FlowError) as result:
        OAuthNavigator(transport,context).complete("https://auth.openai.com"+path)
    assert result.value.code == "OAUTH_PARSE_ERROR"
    assert result.value.details["selection_kind"] == source
    assert result.value.details["candidate_count"] == 0
    assert len(transport.requests) == 1


def test_conflicting_selected_workspace_still_requires_explicit_choice(context):
    a,b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    transport = FakeTransport(Response(text="<html></html>"))
    transport.cookies={"oai-client-auth-session":quote(json.dumps({"workspace_id":a,"workspaces":[{"id":b}]}))}
    with pytest.raises(FlowError):
        OAuthNavigator(transport,context).complete("https://auth.openai.com/consent")
    assert len(transport.requests) == 1


def test_cookie_selected_workspace_must_match_html_workspace(context):
    selected = "11111111-1111-4111-8111-111111111111"
    html_workspace = "22222222-2222-4222-8222-222222222222"
    transport = FakeTransport(Response(text=json.dumps({"workspace_id":html_workspace})))
    transport.cookies = {"oai-client-auth-session":quote(json.dumps({"workspace_id":selected}))}
    with pytest.raises(FlowError) as result:
        OAuthNavigator(transport,context).complete("https://auth.openai.com/consent")
    assert result.value.code == "WORKSPACE_SELECTION_REQUIRED"
    assert result.value.details["candidate_count"] == 2


def test_same_workspace_uuid_case_does_not_make_two_choices(context):
    workspace = "ABCDEFAB-1234-4123-8123-123456789ABC"
    transport = FakeTransport(Response(text=json.dumps({"workspace_id":workspace})),
        Response({"continue_url":"http://localhost:1455/auth/callback?code=c&state=s"}))
    payload = {"workspaces":[{"id":workspace}]}
    transport.cookies = {"oai-client-auth-session":base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")}
    OAuthNavigator(transport,context).complete("https://auth.openai.com/consent")
    assert transport.requests[1][2]["json"] == {"workspace_id":workspace.lower()}


def test_html_workspace_radio_value_is_selected(context):
    workspace = "33333333-3333-4333-8333-333333333333"
    html = ('<fieldset role="radiogroup">'
            f'<input type="radio" disabled name="workspace_id" value="{workspace}" />'
            f'<input type="hidden" name="workspace_id" value="{workspace}" />'
            '<span>Personal account</span></fieldset>')
    transport = FakeTransport(Response(text=html),
        Response({"continue_url":"http://localhost:1455/auth/callback?code=c&state=s"}))
    OAuthNavigator(transport, context).complete("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
    assert transport.requests[1][2]["json"] == {"workspace_id":workspace}


@pytest.mark.parametrize("markup", [
    '<input type="text" name="workspace_id" value="{id}">',
    '<input type="radio" data-name="workspace_id" value="{id}">',
    '<input type="radio" name="account_id" value="{id}">',
    '<input type="radio" name="workspace_id" name="other" value="{id}">',
    '<input type="radio" name=other name="workspace_id" value="{id}">',
    '<input type="radio" name="workspace_id" value value="{id}">',
    '<input type="radio" name="workspace_id" value="bad" value="{id}">',
    '<input type="radio" name="workspace_id" value="invalid">',
    '<input type="radio" name="workspace_id">',
    '<input type="radio" name="workspace_id" value="{id}\'>',
    '<!-- <input type="radio" name="workspace_id" value="{id}"> -->',
    '<script>const example = \'<input type="radio" name="workspace_id" value="{id}">\';</script>',
])
def test_html_workspace_parser_rejects_unrelated_or_malformed_input(context, markup):
    html = markup.format(id="44444444-4444-4444-8444-444444444444")
    transport = FakeTransport(Response(text=html))
    with pytest.raises(FlowError) as err:
        OAuthNavigator(transport, context).complete("https://auth.openai.com/consent")
    assert err.value.code == "OAUTH_PARSE_ERROR"
    assert len(transport.requests) == 1


def test_html_workspace_parser_preserves_multiple_choices(context):
    html = ('<input type="radio" name="workspace_id" value="11111111-1111-4111-8111-111111111111">'
            '<input type="radio" name="workspace_id" value="22222222-2222-4222-8222-222222222222">')
    transport = FakeTransport(Response(text=html))
    with pytest.raises(FlowError) as err:
        OAuthNavigator(transport, context).complete("https://auth.openai.com/consent")
    assert err.value.code == "WORKSPACE_SELECTION_REQUIRED"
    assert err.value.details["candidate_count"] == 2
    assert len(transport.requests) == 1


def test_html_workspace_parser_handles_attribute_order_and_entities(context):
    html = ("<INPUT VALUE='abcdefab-1234-4123-8123-123456789abc' "
            "NAME='workspace&#95;id' TYPE='HIDDEN'>")
    transport = FakeTransport(Response(text=html),
        Response({"continue_url":"http://localhost:1455/auth/callback?code=c&state=s"}))
    OAuthNavigator(transport, context).complete("https://auth.openai.com/consent")
    assert transport.requests[1][2]["json"] == {"workspace_id":"abcdefab-1234-4123-8123-123456789abc"}
