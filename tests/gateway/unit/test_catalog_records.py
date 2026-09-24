from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError


def _stamp():
    from autobuild_json.gateway.catalog.records import ConfigStamp

    return ConfigStamp(
        source_version=1,
        provider_version=2,
        credential_version=3,
        content_digest="a" * 64,
    )


def test_snapshot_digest_is_content_not_observation_time():
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from autobuild_json.gateway.catalog.digests import content_digest

    value = ObservedValue(
        value="Vendor",
        source="upstream",
        path="owned_by",
        observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    a = CatalogEntry(upstream_id="model-a", metadata={"owner": value})
    b = a.model_copy(update={"upstream_id": "model-b"})
    later = value.model_copy(update={"observed_at": datetime(2026, 9, 25, tzinfo=timezone.utc)})
    a2 = a.model_copy(update={"metadata": {"owner": later}})
    assert content_digest((a, b)) == content_digest((b, a2))
    assert content_digest((a,)) != content_digest((b,))


def test_snapshot_digest_includes_provenance_path_and_values():
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from autobuild_json.gateway.catalog.digests import content_digest

    observed = datetime(2026, 9, 24, tzinfo=timezone.utc)
    first = CatalogEntry(upstream_id="m", metadata={"owner": ObservedValue(value="Vendor", path="owned_by", observed_at=observed)})
    changed_path = CatalogEntry(upstream_id="m", metadata={"owner": ObservedValue(value="Vendor", path="publisher", observed_at=observed)})
    changed_value = CatalogEntry(upstream_id="m", metadata={"owner": ObservedValue(value="Other", path="owned_by", observed_at=observed)})
    assert len({content_digest((first,)), content_digest((changed_path,)), content_digest((changed_value,))}) == 3


def test_snapshot_digest_is_order_independent_even_for_repeated_upstream_ids():
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue
    from autobuild_json.gateway.catalog.digests import content_digest

    observed = datetime(2026, 9, 24, tzinfo=timezone.utc)
    first = CatalogEntry(upstream_id="same", metadata={"owner": ObservedValue(value="A", observed_at=observed)})
    second = CatalogEntry(upstream_id="same", metadata={"owner": ObservedValue(value="B", observed_at=observed)})
    assert content_digest((first, second)) == content_digest((second, first))


def test_canonical_bytes_are_stable_unicode_and_exact_decimal():
    from autobuild_json.gateway.catalog.digests import canonical_bytes

    assert canonical_bytes({"é": Decimal("1.2300"), "a": [2, 1]}) == b'{"a":[2,1],"\xc3\xa9":"1.2300"}'
    with pytest.raises(ValueError):
        canonical_bytes({"bad": float("nan")})


def test_operation_digest_excludes_client_id_but_includes_probe_intent():
    from autobuild_json.gateway.catalog.digests import operation_digest
    from autobuild_json.gateway.catalog.records import OperationRequest, ProbeIntent

    source_id = uuid4()
    expected = _stamp()
    check = OperationRequest(id=uuid4(), source_id=source_id, kind="check", expected=expected)
    assert operation_digest(check) == operation_digest(check.model_copy(update={"id": uuid4()}))
    assert operation_digest(check) != operation_digest(check.model_copy(update={"kind": "discover"}))
    probe = ProbeIntent(
        binding_id=uuid4(), quote_digest="b" * 64, expected=expected,
        max_hold=Decimal("0.001"), currency="USD", acknowledged=True,
    )
    request = OperationRequest(id=uuid4(), source_id=source_id, kind="probe", expected=expected, probe=probe)
    assert operation_digest(request) != operation_digest(request.model_copy(update={"probe": probe.model_copy(update={"max_hold": Decimal("0.002")})}))


@pytest.mark.parametrize("interval", [299, 604801, True, "300"])
def test_source_rejects_out_of_range_or_noninteger_schedule(interval):
    from autobuild_json.gateway.catalog.records import SourceInput

    with pytest.raises(ValidationError):
        SourceInput(provider_id=uuid4(), credential_id=uuid4(), mode="openai_single", interval_seconds=interval)


def test_source_has_safe_schedule_defaults_and_no_body_auth_fields():
    from autobuild_json.gateway.catalog.records import SourceInput

    source = SourceInput(provider_id=uuid4(), credential_id=uuid4(), mode="openai_single")
    assert (source.enabled, source.schedule_enabled, source.interval_seconds) == (True, False, 86400)
    with pytest.raises(ValidationError):
        SourceInput(provider_id=uuid4(), credential_id=uuid4(), mode="openai_single", root="https://example.com")


@pytest.mark.parametrize("root", [
    "https://user:example-secret@example.test/v1",
    "https://example.test/v1?key=example-secret",
    "https://example.test/v1#fragment",
])
def test_operation_route_rejects_embedded_credentials_or_dynamic_url_parts(root):
    from autobuild_json.gateway.catalog.records import OperationRoute

    with pytest.raises(ValidationError, match="invalid_provider_root"):
        OperationRoute(
            provider_id=uuid4(), credential_id=uuid4(), root=root,
            adapter="openai_compatible", wire_api="chat", auth_mode="bearer",
            config_version=1, timeout=30,
        )


def test_operation_route_normalizes_safe_root_like_provider_config():
    from autobuild_json.gateway.catalog.records import OperationRoute

    route = OperationRoute(
        provider_id=uuid4(), credential_id=uuid4(), root="https://example.test/v1/",
        adapter="openai_compatible", wire_api="chat", auth_mode="bearer",
        config_version=1, timeout=30,
    )
    assert route.root == "https://example.test/v1"


def test_publication_requires_unique_bounded_explicit_selections():
    from autobuild_json.gateway.catalog.records import PublicationRequest, PublishItem

    item = PublishItem(
        upstream_id="vendor/model", public_model_id="vendor/model", identity="vendor/model",
        input_bound=100, output_bound=16, capabilities=frozenset({"text"}),
    )
    base = dict(id=uuid4(), source_id=uuid4(), run_id=uuid4(), expected=_stamp())
    with pytest.raises(ValidationError):
        PublicationRequest(**base, selections=())
    with pytest.raises(ValidationError):
        PublicationRequest(**base, selections=(item,) * 101)
    with pytest.raises(ValidationError):
        PublicationRequest(**base, selections=(item, item))
    with pytest.raises(ValidationError):
        PublishItem(upstream_id="vendor/model", public_model_id="vendor/model", identity="vendor/model", input_bound=100, output_bound=16)
    assert item.capabilities == frozenset({"text"})


def test_probe_requires_acknowledgement_and_bounded_decimal():
    from autobuild_json.gateway.catalog.records import ProbeIntent

    base = dict(binding_id=uuid4(), quote_digest="b" * 64, expected=_stamp(), currency="USD")
    with pytest.raises(ValidationError):
        ProbeIntent(**base, max_hold=Decimal("0.01"))
    for amount in (Decimal("1e20"), Decimal("NaN"), Decimal("-1")):
        with pytest.raises(ValidationError):
            ProbeIntent(**base, max_hold=amount, acknowledged=True)


def test_records_are_strict_frozen_and_require_utc_aware_dates():
    from autobuild_json.gateway.catalog.records import CatalogEntry, ObservedValue

    with pytest.raises(ValidationError):
        ObservedValue(value="x", observed_at=datetime(2026, 9, 24))
    with pytest.raises(ValidationError):
        CatalogEntry(upstream_id="x", metadata={}, unexpected=True)
    entry = CatalogEntry(upstream_id="x")
    with pytest.raises(ValidationError):
        entry.upstream_id = "y"
