import json

import pytest
from curl_cffi.requests.exceptions import Timeout

from autobuild_json.challenges import inspect_response
from autobuild_json.errors import FlowError
from autobuild_json.input_parser import parse_accounts
from autobuild_json.models import RunResult
from autobuild_json.results import RunStore
from autobuild_json.transport import SafeTransport
from tests.fakes import Response
from tests.test_transport import Raw


def test_diagnostics_reject_secrets_and_survive_result_export(tmp_path):
    error = FlowError("EMAIL_OTP_REQUIRED", "authorize", details={
        "http_status":302, "endpoint":"/api/accounts/authorize", "host":"auth.openai.com",
        "method":"GET", "redirect_path":"/email-verification", "evidence":"redirect",
        "password":"private-password", "cookie":"private-cookie", "message":"private-message",
        "page_type":"secret-email@example.com", "curl_code":"private-string",
    })
    report = parse_accounts("u@example.com|private-password|JBSWY3DPEHPK3PXP")
    store = RunStore(tmp_path)
    job = store.create(report)
    store.update(job, report.accounts[0].account_job_id, status="error", stage="authorize", attempt=1,
                 result=RunResult("error", error=error))
    row = json.loads(store.export(job,"errors"))[0]
    assert row["details"] == {"http_status":302, "endpoint":"/api/accounts/authorize", "host":"auth.openai.com",
        "method":"GET", "redirect_path":"/email-verification", "evidence":"redirect"}
    assert store.snapshot(job)["rows"][0]["details"] == row["details"]
    assert "private-" not in repr(row) and "secret-email" not in repr(row)


def test_email_redirect_records_evidence_not_claiming_wrong_totp():
    response = Response(status=302,location="/email-verification?email=private@example.com&state=secret")
    with pytest.raises(FlowError) as result:
        inspect_response(response,"https://auth.openai.com/api/accounts/authorize?code=secret")
    assert result.value.code == "EMAIL_OTP_REQUIRED"
    assert result.value.details["redirect_path"] == "/email-verification"
    assert result.value.details["http_status"] == 302
    assert result.value.details["evidence"] == "redirect"
    assert "secret" not in repr(result.value.details)


def test_actual_page_type_distinct_from_available_methods():
    inspect_response(Response({"page":{"type":"login_password","payload":{"methods":["email_otp","totp"]}}}),
                     "https://auth.openai.com/log-in/password")
    with pytest.raises(FlowError) as result:
        inspect_response(Response({"page":{"type":"email_otp_verification"}}),"https://auth.openai.com/api/accounts/authorize/continue")
    assert result.value.details["page_type"] == "email_otp_verification"
    assert result.value.details["evidence"] == "page_type"


@pytest.mark.parametrize("status,expected", [(403,"AUTH_BLOCKED"),(404,"HTTP_ERROR"),(500,"HTTP_ERROR"),(503,"HTTP_ERROR")])
def test_http_error_is_not_automatically_provider_block(status, expected):
    with pytest.raises(FlowError) as result:
        inspect_response(Response(status=status),"https://auth.openai.com/api/accounts/password/verify")
    assert result.value.code == expected
    assert result.value.details["http_status"] == status
    assert result.value.details["endpoint"] == "/api/accounts/password/verify"


@pytest.mark.parametrize("exception,code,curl_code", [(Timeout("private URL and cookie",code=28),"REQUEST_TIMEOUT",28),
    (TypeError("private local bug"),"UNEXPECTED_ERROR",None)])
def test_transport_errors_have_actual_type_and_not_raw_exception(context, exception, code, curl_code):
    class Failing(Raw):
        def request(self, **kwargs):
            raise exception
    with pytest.raises(FlowError) as result:
        SafeTransport(None,context,Failing()).request("GET","https://chatgpt.com/api/auth/providers?private=secret")
    assert result.value.code == code
    assert result.value.details["endpoint"] == "/api/auth/providers"
    assert result.value.details["method"] == "GET"
    assert result.value.details.get("curl_code") == curl_code
    assert "private" not in str(result.value) and "secret" not in repr(result.value.details)
