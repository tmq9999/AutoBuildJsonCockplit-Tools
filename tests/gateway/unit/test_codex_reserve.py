from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from autobuild_json.gateway.accounts.quota_client import parse_usage
from autobuild_json.gateway.accounts.quota_records import CodexQuotaSnapshot
from autobuild_json.gateway.accounts.reserve import reserve_diagnostic
from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.providers.codex_model_catalog import client_model
from .test_account_pool import NOW, candidate, ordered, policy


def reserve_payload():
    return {'rate_limit': {'allowed': False, 'limit_reached': True, 'primary_window': {'used_percent': 100}},
            'rate_limit_upsell': {'banner_type': 'luna_reserve'},
            'additional_rate_limits': [{'limit_name': 'gpt-reserve', 'metered_feature': 'reserve',
                'rate_limit': {'allowed': True, 'limit_reached': False,
                               'primary_window': {'used_percent': 25, 'limit_window_seconds': 18000,
                                                  'reset_after_seconds': 3600}}}]}


def reserve_candidate(payload=None):
    item = candidate(1)
    quota = parse_usage(payload or reserve_payload(), item.route.credential_id, NOW)
    return item.model_copy(update={'route': replace(item.route, upstream_model='gpt-reserve'), 'quota': quota})


@pytest.mark.parametrize('name', ['gpt-reserve', 'GPT Reserve', 'gpt_reserve', 'GPTReserve', 'gpt-reserve-daily'])
@pytest.mark.parametrize('shape', ['list', 'object', 'camel', 'object_key'])
def test_reserve_parses_named_limits_without_losing_normal_exhaustion(name, shape):
    payload = reserve_payload()
    entry = payload['additional_rate_limits'][0]
    entry['limit_name'] = name
    if shape in {'object', 'object_key'}:
        if shape == 'object_key':
            entry.pop('limit_name')
        payload['additional_rate_limits'] = {name: entry}
    if shape == 'camel':
        entry['limitName'] = entry.pop('limit_name')
        entry['rateLimit'] = entry.pop('rate_limit')
        payload['additionalRateLimits'] = payload.pop('additional_rate_limits')
    item = reserve_candidate(payload)
    assert item.quota.allowed is False and item.quota.limit_reached is True
    limit = item.quota.additional_rate_limits[0]
    assert limit.model_id == 'gpt-reserve' and limit.allowed is True
    assert limit.primary.used_percent == 25 and limit.primary.reset_at == NOW + timedelta(hours=1)
    assert ordered(policy(item), [item]) == [1]
    assert reserve_diagnostic(item.quota, item.route.credential_id, NOW) is None
    assert CodexQuotaSnapshot.model_validate_json(item.quota.model_dump_json()) == item.quota


@pytest.mark.parametrize('mutation', ['regular_allowed', 'regular_unknown', 'missing', 'banner_missing',
    'banner_other', 'reserve_denied', 'reserve_unknown', 'reserve_exhausted', 'window_exhausted',
    'stale', 'error', 'different_account', 'future'])
def test_reserve_entitlement_missing_stale_conflicting_never_selects(mutation):
    payload = reserve_payload()
    if mutation == 'regular_allowed':
        payload['rate_limit']['allowed'] = True
    if mutation == 'regular_unknown':
        payload['rate_limit'].pop('allowed')
    if mutation == 'missing':
        payload.pop('additional_rate_limits')
    if mutation == 'banner_missing':
        payload.pop('rate_limit_upsell')
    if mutation == 'banner_other':
        payload['rate_limit_upsell']['banner_type'] = 'other'
    if mutation == 'reserve_denied':
        payload['additional_rate_limits'][0]['rate_limit']['allowed'] = False
    if mutation == 'reserve_unknown':
        payload['additional_rate_limits'][0]['rate_limit'].pop('allowed')
    if mutation == 'reserve_exhausted':
        payload['additional_rate_limits'][0]['rate_limit']['limit_reached'] = True
    if mutation == 'window_exhausted':
        payload['additional_rate_limits'][0]['rate_limit']['primary_window']['used_percent'] = 100
    item = reserve_candidate(payload)
    changes = {'stale': {'fetched_at': NOW-timedelta(seconds=121)}, 'error': {'last_error': 'codex_usage_unavailable'},
               'different_account': {'credential_id': candidate(2).route.credential_id},
               'future': {'fetched_at': NOW+timedelta(seconds=1)}}
    if mutation in changes:
        item = item.model_copy(update={'quota': item.quota.model_copy(update=changes[mutation])})
    assert ordered(policy(item), [item]) == []


@pytest.mark.parametrize('bad', [[], 'yes', 1])
def test_non_boolean_reserve_permission_fails_closed(bad):
    payload = reserve_payload()
    payload['additional_rate_limits'][0]['rate_limit']['allowed'] = bad
    with pytest.raises(GatewayError):
        reserve_candidate(payload)


def test_alias_conflicts_duplicate_limits_and_oversize_are_rejected():
    payload = reserve_payload()
    payload['additionalRateLimits'] = []
    with pytest.raises(GatewayError):
        reserve_candidate(payload)
    payload = reserve_payload()
    payload['additional_rate_limits'] *= 2
    with pytest.raises(GatewayError):
        reserve_candidate(payload)
    payload = reserve_payload()
    payload['additional_rate_limits'] *= 101
    with pytest.raises(GatewayError):
        reserve_candidate(payload)


def test_old_snapshots_and_missing_windows_never_fabricate_reserve():
    item = candidate(1, '0')
    quota = CodexQuotaSnapshot.model_validate_json(item.quota.model_dump_json(exclude={'allowed','banner_type','additional_rate_limits'}))
    assert quota.additional_rate_limits == () and quota.allowed is None
    assert reserve_diagnostic(quota, item.route.credential_id, NOW) == 'pool_reserve_unavailable'
    payload = reserve_payload()
    payload['additional_rate_limits'][0]['rate_limit'].pop('primary_window')
    item = reserve_candidate(payload)
    assert item.quota.additional_rate_limits[0].primary is None
    assert ordered(policy(item), [item]) == [1]  # Explicit allowed is not an invented percentage.


def test_regular_routes_do_not_consume_reserve_and_quota_floor_is_separate():
    item = reserve_candidate()
    regular = item.model_copy(update={'route': replace(item.route, upstream_model='gpt-5.6-luna')})
    assert ordered(policy(regular), [regular]) == []
    assert ordered(policy(item, reserve_enabled=True, min_primary_remaining=Decimal(100)), [item]) == [1]
    assert ordered(policy(candidate(2)), [item]) == []


def test_reserve_catalog_uses_luna_metadata_and_preserves_model_id():
    reserve, luna = client_model('gpt-reserve'), client_model('gpt-5.6-luna')
    assert reserve['slug'] == 'gpt-reserve' and reserve['display_name'] == 'GPT-5.6 Reserve'
    for key in ('context_window', 'max_context_window', 'auto_compact_token_limit', 'supported_reasoning_levels'):
        assert reserve[key] == luna[key]


def test_reserve_auto_order_uses_additional_not_exhausted_normal_windows():
    low = reserve_candidate()
    high = reserve_candidate()
    second = candidate(2).route
    quota = high.quota.model_copy(update={'credential_id': second.credential_id,
        'additional_rate_limits': (high.quota.additional_rate_limits[0].model_copy(update={
            'primary': high.quota.additional_rate_limits[0].primary.model_copy(update={'used_percent': Decimal(10)})}),)})
    high = high.model_copy(update={'route': replace(second, upstream_model='gpt-reserve'), 'quota': quota})
    assert ordered(policy(low, high, mode='auto'), [low, high]) == [2, 1]


@pytest.mark.parametrize('bad', [[], False, '', 0])
def test_malformed_upsell_is_rejected(bad):
    payload = reserve_payload()
    payload['rate_limit_upsell'] = bad
    with pytest.raises(GatewayError):
        reserve_candidate(payload)
