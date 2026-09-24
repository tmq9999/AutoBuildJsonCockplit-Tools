"""Synthetic provider list responses; no live provider account is used."""

from datetime import datetime, timezone

import pytest


OBSERVED = datetime(2026, 9, 24, tzinfo=timezone.utc)


@pytest.mark.parametrize("payload,code", [
    ({"error": "private-error"}, "upstream_error"),
    ({"data": [], "has_more": True}, "catalog_incomplete"),
])
def test_openai_single_page_never_hides_missing_pages(payload, code):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    with pytest.raises(GatewayError, match=code):
        parse_page("openai_single", payload, OBSERVED)


@pytest.mark.parametrize("mode,payload,expected_id,path", [
    ("openai_single", {"object": "list", "data": [{"id": "gpt-x", "owned_by": "vendor"}]}, "gpt-x", "owned_by"),
    ("openai_cursor", {"data": [{"id": "relay-x", "owned_by": "relay"}], "has_more": False}, "relay-x", "owned_by"),
    ("anthropic", {"data": [{"id": "claude-x", "display_name": "Claude X"}], "has_more": False, "last_id": "claude-x"}, "claude-x", "display_name"),
    ("gemini", {"models": [{"name": "models/gemini-x", "displayName": "Gemini X"}]}, "gemini-x", "displayName"),
    ("ollama", {"models": [{"name": "llama:latest", "model": "llama:latest", "modified_at": "2026-09-24T00:00:00Z"}]}, "llama:latest", "modified_at"),
])
def test_native_shapes_preserve_identity_and_metadata_provenance(mode, payload, expected_id, path):
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page(mode, payload, OBSERVED)
    assert [entry.upstream_id for entry in page.entries] == [expected_id]
    assert page.complete is True
    assert page.next_cursor is None
    assert any(observed.path == path and observed.observed_at == OBSERVED and observed.source == "upstream"
               for observed in page.entries[0].metadata.values())


@pytest.mark.parametrize("mode,payload,cursor", [
    ("openai_cursor", {"data": [{"id": "x"}], "has_more": True, "last_id": "x"}, "x"),
    ("anthropic", {"data": [{"id": "x"}], "has_more": True, "last_id": "x"}, "x"),
    ("gemini", {"models": [{"name": "models/x"}], "nextPageToken": "page-two"}, "page-two"),
])
def test_native_pagination_requires_explicit_next_cursor(mode, payload, cursor):
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page(mode, payload, OBSERVED)
    assert page.next_cursor == cursor
    assert page.complete is False


@pytest.mark.parametrize("mode,payload", [
    ("openai_single", {"data": [], "next": "https://other.invalid/models"}),
    ("openai_single", {"data": [], "nextPageToken": "x"}),
    ("openai_cursor", {"data": [], "has_more": True}),
    ("openai_cursor", {"data": []}),
    ("anthropic", {"data": [], "has_more": 1}),
    ("anthropic", {"data": [], "has_more": True, "last_id": ""}),
    ("gemini", {"models": [], "nextPageToken": 7}),
    ("ollama", {"models": [{"name": "a", "model": "b"}]}),
    ("gemini", {"models": [{"name": "models/"}]}),
    ("openai_single", {"nested": {"data": [{"id": "hidden"}]}}),
])
def test_unreadable_or_incomplete_page_fails_closed(mode, payload):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    with pytest.raises(GatewayError, match="catalog_incomplete|upstream_error"):
        parse_page(mode, payload, OBSERVED)


@pytest.mark.parametrize("mode,payload", [
    ("openai_single", {"data": [{"id": True}]}),
    ("openai_single", {"data": [{"id": "x\nsecret"}]}),
    ("gemini", {"models": [{"name": "models/é"}, {"name": "é"}]}),
    ("openai_single", {"data": [{"id": "x", "owned_by": "a"}, {"id": "x", "owned_by": "b"}]}),
    ("ollama", {"models": [{"name": "x", "model": "x"}, {"name": "x", "model": "y"}]}),
])
def test_invalid_ids_and_conflicting_semantic_duplicates_fail_whole_page(mode, payload):
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    with pytest.raises(GatewayError, match="upstream_error"):
        parse_page(mode, payload, OBSERVED)


def test_identical_duplicate_rows_collapse_and_unicode_id_survives():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("openai_single", {"data": [{"id": "mô hình"}, {"id": "mô hình"}]}, OBSERVED)
    assert [entry.upstream_id for entry in page.entries] == ["mô hình"]


def test_secret_reflection_is_rejected_without_repeating_secret():
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    secret = "synthetic-private-credential"
    with pytest.raises(GatewayError) as exc:
        parse_page("openai_single", {"data": [{"id": "x", "owned_by": secret}]}, OBSERVED, secret=secret)
    assert secret not in str(exc.value)


def test_optional_invalid_metadata_is_unknown_and_reports_bounded_warning():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("gemini", {"models": [{"name": "models/x", "displayName": "<script>alert(1)</script>",
        "inputTokenLimit": True, "outputTokenLimit": 1_000_000_001,
        "supportedGenerationMethods": ["generateContent", 7]}]}, OBSERVED)
    entry = page.entries[0]
    assert entry.metadata["name"].value == "<script>alert(1)</script>"
    assert "input_token_limit" not in entry.metadata
    assert "output_token_limit" not in entry.metadata
    assert "supported_generation_methods" not in entry.metadata
    assert page.warnings == ("malformed_optional_metadata",)


def test_explicit_relay_price_and_capabilities_are_validated_without_inference():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("openai_cursor", {"data": [{"id": "x", "pricing": {
        "currency": "USD", "unit": "per_million_tokens", "input": "0.25", "output": "1.50"},
        "capabilities": ["text", "tools"]}], "has_more": False}, OBSERVED)
    metadata = page.entries[0].metadata
    assert metadata["pricing"].value == {"currency": "USD", "unit": "per_million_tokens", "input": "0.25", "output": "1.50"}
    assert metadata["capabilities"].value == ["text", "tools"]
    assert metadata["pricing"].path == "pricing"


@pytest.mark.parametrize("price", [
    {"currency": "USD", "unit": "per_request", "input": "1", "output": "1"},
    {"currency": "USD", "unit": "per_token", "input": True, "output": "1"},
    {"currency": "USD", "unit": "per_token", "input": "NaN", "output": "1"},
])
def test_invalid_optional_pricing_never_enters_catalog(price):
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("openai_cursor", {"data": [{"id": "x", "pricing": price}], "has_more": False}, OBSERVED)
    assert "pricing" not in page.entries[0].metadata
    assert page.warnings == ("malformed_optional_metadata",)


def test_unsupported_media_does_not_become_relay_capability():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("openai_cursor", {"data": [{"id": "x", "capabilities": ["text", "telepathy"]}], "has_more": False}, OBSERVED)
    assert "capabilities" not in page.entries[0].metadata
    assert page.warnings == ("malformed_optional_metadata",)


def test_unhashable_optional_price_unit_is_unknown_instead_of_crashing():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    price = {"currency": "USD", "unit": [], "input": "1", "output": "1"}
    page = parse_page("openai_cursor", {"data": [{"id": "x", "pricing": price}], "has_more": False}, OBSERVED)
    assert "pricing" not in page.entries[0].metadata
    assert page.warnings == ("malformed_optional_metadata",)


def test_empty_explicit_method_list_is_preserved_without_capability_inference():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("gemini", {"models": [{"name": "models/x", "supportedGenerationMethods": []}]}, OBSERVED)
    assert page.entries[0].metadata["supported_generation_methods"].value == []
    assert "capabilities" not in page.entries[0].metadata
    assert page.warnings == ()


@pytest.mark.parametrize("field,value", [
    ("owned_by", "x" * 201),
    ("created", True),
    ("shutdown_date", "2026-02-30"),
    ("modalities", ["telepathy"]),
])
def test_invalid_optional_openai_metadata_is_omitted(field, value):
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("openai_single", {"data": [{"id": "x", field: value}]}, OBSERVED)
    assert set(page.entries[0].metadata) == {"id"}
    assert page.warnings == ("malformed_optional_metadata",)


def test_parser_detaches_source_metadata_and_keeps_literal_display_text():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    methods = ["generateContent"]
    payload = {"models": [{"name": "models/é", "displayName": "<img src=x onerror=alert(1)>",
                           "supportedGenerationMethods": methods, "inputTokenLimit": 1024}]}
    page = parse_page("gemini", payload, OBSERVED)
    methods.append("countTokens")
    payload["models"][0]["displayName"] = "changed"
    assert page.entries[0].upstream_id == "é"
    assert page.entries[0].metadata["name"].value == "<img src=x onerror=alert(1)>"
    assert page.entries[0].metadata["supported_generation_methods"].value == ["generateContent"]
    assert page.entries[0].metadata["input_token_limit"].value == 1024


def test_bytes_payload_or_id_is_unreadable():
    from autobuild_json.gateway.errors import GatewayError
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    for payload in (b'{}', {"data": [{"id": b"x"}]}):
        with pytest.raises(GatewayError, match="upstream_error"):
            parse_page("openai_single", payload, OBSERVED)


def test_explicit_openai_null_shutdown_retains_source_path():
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page("openai_single", {"data": [{"id": "x", "shutdown_date": None}]}, OBSERVED)
    observed = page.entries[0].metadata["shutdown"]
    assert (observed.value, observed.path, observed.observed_at) == (None, "shutdown_date", OBSERVED)


@pytest.mark.parametrize("mode,payload", [
    ("openai_single", {"data": []}),
    ("anthropic", {"data": [], "has_more": False}),
    ("gemini", {"models": []}),
    ("ollama", {"models": []}),
])
def test_valid_empty_page_is_distinct_from_unreadable_page(mode, payload):
    from autobuild_json.gateway.providers.discovery.parsers import parse_page

    page = parse_page(mode, payload, OBSERVED)
    assert page.entries == ()
    assert page.complete is True
