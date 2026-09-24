"""Discovery requests use only fixed model-list endpoints and allowlisted queries."""

from urllib.parse import parse_qs
import time
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.parametrize("mode,suffix,query", [
    ("openai_single", "models", ()),
    ("openai_cursor", "models", (("limit", "100"),)),
    ("anthropic", "models", (("limit", "100"),)),
    ("gemini", "models", (("pageSize", "100"),)),
    ("ollama", "tags", ()),
])
def test_discovery_request_uses_fixed_path_get_and_page_size(mode, suffix, query):
    from autobuild_json.gateway.transport.discovery import discovery_request

    request = discovery_request(mode, None, None, time.monotonic() + 10)
    assert (request.kind, request.discovery_mode, request.method, request.suffix, request.body, request.query) == (
        "discovery", mode, "GET", suffix, None, query)


@pytest.mark.parametrize("mode,key", [
    ("openai_cursor", "after"), ("anthropic", "after_id"), ("gemini", "pageToken"),
])
def test_cursor_is_literal_query_value_and_url_encoded_by_transport(mode, key):
    from autobuild_json.gateway.transport.discovery import discovery_request

    cursor = "&key=stolen?alt=sse/é"
    request = discovery_request(mode, cursor, ("Authorization", "Bearer synthetic"), time.monotonic() + 10)
    expected = (("pageSize", "100"), (key, cursor)) if mode == "gemini" else (("limit", "100"), (key, cursor))
    assert request.query == expected
    assert request.auth_header == ("Authorization", "Bearer synthetic")


@pytest.mark.asyncio
async def test_transport_never_reinterprets_cursor_as_key_or_second_query_field():
    from autobuild_json.gateway.transport.discovery import discovery_request
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.http import Transport

    async def resolver(host, port):
        return ["93.184.216.34"]

    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [], "has_more": False})

    transport = Transport(EgressPolicy(resolver=resolver), adapter=httpx.MockTransport(respond))
    request = discovery_request("openai_cursor", "&key=stolen?alt=sse/é", None, time.monotonic() + 10)
    async with transport.open(SimpleNamespace(root="https://provider.invalid/v1"), None, request):
        pass
    assert seen[0].url.path == "/v1/models"
    assert parse_qs(seen[0].url.query.decode()) == {"limit": ["100"], "after": ["&key=stolen?alt=sse/é"]}


@pytest.mark.parametrize("query", [
    (("after", "x"), ("after", "y")),
    (("limit", "100"), ("key", "bad")),
    (("limit", "100"), ("alt", "sse")),
    (("limit", "100"), ("after_id", "x")),
])
def test_discovery_query_rejects_duplicates_and_foreign_keys(query):
    from autobuild_json.gateway.transport.http import OutboundRequest

    with pytest.raises(ValueError, match="invalid_upstream_query"):
        OutboundRequest("GET", "models", None, deadline=time.monotonic() + 10,
                        kind="discovery", discovery_mode="openai_cursor", query=query)


def test_discovery_query_rejects_oversize_page_size_without_integer_conversion_error():
    from autobuild_json.gateway.transport.http import OutboundRequest

    with pytest.raises(ValueError, match="invalid_upstream_query"):
        OutboundRequest("GET", "models", None, kind="discovery", discovery_mode="anthropic",
                        query=(("limit", "9" * 5000),))


@pytest.mark.parametrize("cursor", ["", "x" * 2049, "x\nadmin", "x\x7fadmin", "x\x85admin", b"bytes"])
def test_discovery_cursor_rejects_empty_oversize_control_or_bytes(cursor):
    from autobuild_json.gateway.transport.discovery import discovery_request

    with pytest.raises(ValueError, match="invalid_cursor"):
        discovery_request("anthropic", cursor, None, time.monotonic() + 10)


def test_discovery_rejects_wrong_method_body_path_and_mode():
    from autobuild_json.gateway.transport.http import OutboundRequest

    base = dict(deadline=time.monotonic() + 10, kind="discovery", discovery_mode="gemini")
    for method, suffix, body in [("POST", "models", None), ("GET", "models", {}), ("GET", "other", None)]:
        with pytest.raises(ValueError):
            OutboundRequest(method, suffix, body, **base)
    with pytest.raises(ValueError):
        OutboundRequest("GET", "models", None, kind="discovery", discovery_mode="unknown")


def test_inference_query_remains_only_alt_sse():
    from autobuild_json.gateway.transport.http import OutboundRequest

    request = OutboundRequest("POST", "chat/completions", {}, query=(("alt", "sse"),))
    assert request.kind == "inference"
    with pytest.raises(ValueError):
        OutboundRequest("GET", "models", None, query=(("after", "x"),))
