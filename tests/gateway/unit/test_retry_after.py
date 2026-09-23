from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from autobuild_json.gateway.contracts import GenerationOptions, InferenceRequest
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.providers.anthropic import AnthropicAdapter
from autobuild_json.gateway.providers.codex import CodexAdapter
from autobuild_json.gateway.providers.gemini import GeminiAdapter
from autobuild_json.gateway.providers.ollama import OllamaAdapter
from autobuild_json.gateway.providers.openai import OpenAIAdapter
from autobuild_json.gateway.transport.egress import EgressPolicy
from autobuild_json.gateway.transport.http import Transport


@pytest.mark.parametrize("adapter_class", [OpenAIAdapter, AnthropicAdapter, GeminiAdapter, OllamaAdapter, CodexAdapter])
@pytest.mark.parametrize("hint,expected", [("120", 120), ("0", 0), (" 17 ", 17), ("99999999999999999999", 86400)])
@pytest.mark.asyncio
async def test_rejected_generation_preserves_safe_delay_without_replaying(adapter_class, hint, expected):
    # Dropping a header or mapping provider 429 to 502 breaks the client contract.
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": hint}, json={"error": "private-upstream-body"})

    async def resolve(host, port):
        return ["93.184.216.34"]

    async def credential(route):
        if adapter_class is CodexAdapter:
            return "synthetic-account", {"access_token": "synthetic-only"}
        return "synthetic-only"

    transport = Transport(EgressPolicy(resolver=resolve), adapter=httpx.MockTransport(respond))
    route = SimpleNamespace(root="https://chatgpt.com/backend-api/codex" if adapter_class is CodexAdapter
                            else "https://provider.invalid/v1", upstream_model="model", timeout=30,
                            auth_mode="bearer", wire_api="chat")
    lease = SimpleNamespace(proxy=None, deadline=datetime.now(timezone.utc) + timedelta(seconds=30))
    request = InferenceRequest(model="public", options=GenerationOptions(
        max_output_tokens=None if adapter_class is CodexAdapter else 8))
    with pytest.raises(GatewayError) as caught:
        async with adapter_class(transport, credential).open(request, route, lease):
            pytest.fail("A rejected request must not expose a stream")
    assert caught.value.retry_after == expected
    assert caught.value.status == 429
    assert caught.value.code == "rate_limited"
    assert len(calls) == 1
    assert "private-upstream-body" not in str(caught.value.to_dict())


@pytest.mark.parametrize("header,expected", [
    (None, None), ("", None), ("-1", None), ("1.5", None), ("+10", None),
    ("NaN", None), ("１２", None), ("١٢", None), ("12\r\nX-Private: leak", None),
    ("not-a-date", None), pytest.param("9" * 5000, None, id="oversized"),
    ("0", 0), (" 009\t", 9), ("86401", 86400),
    ("Thu, 24 Sep 2026 00:02:00 GMT", 120),
    ("Thu, 24 Sep 2026 00:00:00 GMT", 0),
    ("Wed, 23 Sep 2026 23:59:00 GMT", 0),
    ("Sat, 26 Sep 2026 00:00:00 GMT", 86400),
    ("Thursday, 24-Sep-26 00:02:00 GMT", 120),
    ("Thu Sep 24 00:02:00 2026", 120),
    ("Thu, 24 Sep 2026 00:02:00 GMT, 15", None),
    ("Thu, 24 Sep 2026 00:02:00 GMT garbage", None),
    ("24 Sep 2026 00:02:00", None),
    ("Thu, 24 Sep 2026 00:02:00 JUNK", None),
    ("Fri, 32 Sep 2026 00:02:00 GMT", None),
])
def test_retry_after_parser_has_bounded_strict_inputs(header, expected):
    from autobuild_json.gateway.providers.retry import parse_retry_after
    assert parse_retry_after(header, now=datetime(2026, 9, 24, tzinfo=timezone.utc)) == expected


def test_http_date_delay_rounds_up_instead_of_retrying_early():
    from autobuild_json.gateway.providers.retry import parse_retry_after
    now = datetime(2026, 9, 24, microsecond=750000, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 24 Sep 2026 00:00:02 GMT", now=now) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_class", [OpenAIAdapter, AnthropicAdapter, GeminiAdapter, OllamaAdapter, CodexAdapter])
async def test_http_date_header_reaches_generation_error(adapter_class, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 24, tzinfo=timezone.utc)
    monkeypatch.setattr("autobuild_json.gateway.providers.retry.datetime", FrozenDateTime)
    future = format_datetime(datetime(2026, 9, 24, 0, 2, tzinfo=timezone.utc), usegmt=True)
    await test_rejected_generation_preserves_safe_delay_without_replaying(adapter_class, future, 120)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_rate_limit_rejection_does_not_advertise_retry(status):
    from autobuild_json.gateway.errors import UpstreamRejected
    error = UpstreamRejected(status, 120)
    assert error.status == 502 and not error.safe_retry
    assert error.retry_after is None
