from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import time

import httpx
import pytest

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.transport.http import OutboundRequest, Transport
from .test_gateway_transport import policy

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def usage(payload):
    from autobuild_json.gateway.accounts.quota_client import parse_usage
    return parse_usage(payload, uuid4(), NOW)


def credits(payload):
    from autobuild_json.gateway.accounts.quota_client import parse_credits
    return parse_credits(payload, uuid4(), NOW)


def test_missing_windows_unknown_and_weekly_primary_retained():
    assert usage({}).primary is None
    result = usage({'rate_limit': {'primary_window': {'used_percent': 25, 'limit_window_seconds': 604800}}})
    assert Decimal(100) - result.primary.used_percent == Decimal(75)
    assert result.primary.window_seconds == 604800
    assert result.secondary is None


@pytest.mark.parametrize('value', [-1, 101, float('nan'), float('inf'), True, '25'])
def test_invalid_percent_is_not_clamped(value):
    with pytest.raises(GatewayError, match='codex_usage_unavailable'):
        usage({'rate_limit': {'primary_window': {'used_percent': value}}})


def test_absolute_reset_wins_and_relative_is_receipt_based():
    assert usage({'rate_limit': {'primary_window': {'reset_at': int(NOW.timestamp()), 'reset_after_seconds': 30}}}).primary.reset_at == NOW
    assert usage({'rate_limit': {'primary_window': {'reset_after_seconds': 30}}}).primary.reset_at == NOW + timedelta(seconds=30)
    assert usage({'rate_limit': {'primary_window': {'reset_at': '2026-09-25T00:00:00Z'}}}).primary.reset_at == NOW


@pytest.mark.parametrize('window', [{'reset_after_seconds': -1}, {'reset_at': 1.2}, {'reset_at': '2026-09-25'}, {'limit_window_seconds': -1}])
def test_malformed_window_rejected(window):
    with pytest.raises(GatewayError, match='codex_usage_unavailable'):
        usage({'rate_limit': {'primary_window': window}})


def test_credit_unknown_status_never_derives_spendable_count():
    result = credits({'credits': [{'id': 'a'}, {'id': 'b', 'status': 'future'}, {'id': 'c', 'status': 'available'},
                                  {'id': 'd', 'state': 'available', 'expires_at': int((NOW + timedelta(seconds=20)).timestamp())},
                                  {'id': 'e', 'state': 'available', 'expires_at': int((NOW - timedelta(seconds=20)).timestamp())}]})
    assert result.available_count is None
    assert [r.state for r in result.credits] == ['unknown', 'unknown', 'available', 'available', 'available']
    assert credits({}).available_count is None
    assert credits({'data': {'availableCount': 2}}).available_count == 2


def test_explicit_aggregate_count_is_authoritative_with_incomplete_details():
    assert credits({'available_count': 0, 'credits': []}).available_count == 0
    assert credits({'available_count': 2, 'credits': [{'status': 'future'}]}).available_count == 2


def test_fully_classified_details_derive_exact_count_without_aggregate():
    result = credits({'credits': [{'status': 'available', 'expires_at': int((NOW + timedelta(seconds=20)).timestamp())},
                                  {'state': 'redeemed', 'expires_at': int((NOW + timedelta(seconds=20)).timestamp())}]})
    assert result.available_count == 1


@pytest.mark.parametrize('payload', [{'available_count': -1}, {'available_count': True}, {'available_count': 1, 'availableCount': 2},
                                    {'available_count': 1, 'credits': [{'status': 'expired', 'expires_at': int(NOW.timestamp())}]}, {'credits': [{'status': 'available', 'state': 'redeemed'}]},
                                    {'credits': [{'expires_at': '2026-09-25T00:00:00'}]}])
def test_conflicting_or_malformed_credits_rejected(payload):
    with pytest.raises(GatewayError, match='codex_credits_unavailable'):
        credits(payload)


@pytest.mark.parametrize('records', [[], [{'state': 'unknown'}], [{'state': 'available'}],
                                     [{'state': 'expired'}], [{'state': 'redeemed'}]])
def test_incomplete_credit_details_do_not_invent_zero(records):
    assert credits({'credits': records}).available_count is None
    assert credits({'available_count': 0, 'credits': records}).available_count == 0
    assert credits({'available_count': 2, 'credits': records}).available_count == 2


def test_classified_nonavailable_details_can_prove_zero():
    assert credits({'credits': [{'state': 'expired', 'expires_at': int(NOW.timestamp())}]}).available_count == 0


def account_call(**changes):
    args = dict(method='GET', suffix='usage', body=None, kind='codex_account',
                auth_header=('Authorization', 'Bearer synthetic'), headers=(('chatgpt-account-id', 'account'),), deadline=time.monotonic()+10)
    return OutboundRequest(**(args | changes))


@pytest.mark.parametrize('changes', [{'suffix': 'usage/extra'}, {'query': (('x', 'y'),)}, {'body': {}}, {'method': 'POST'},
                                    {'headers': (('cookie', 'x'),)}, {'headers': (('originator', 'x'),)}, {'auth_header': ('x-api-key', 'x')},
                                    {'auth_header': None}, {'headers': ()}, {'method': 'POST', 'suffix': 'rate-limit-reset-credits/consume', 'body': {'x': 'y'}}])
def test_account_request_shape_rejects_untrusted_inputs(changes):
    with pytest.raises(ValueError):
        account_call(**changes)


@pytest.mark.asyncio
async def test_account_root_exact_no_redirect_or_cookies():
    requests = []
    def reply(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'https://elsewhere.invalid', 'set-cookie': 'session=secret'})
    transport = Transport(await policy(), adapter=httpx.MockTransport(reply))
    for root in ['https://chatgpt.com/backend-api/wham/', 'https://elsewhere.invalid/backend-api/wham', 'https://chatgpt.com/backend-api/codex']:
        with pytest.raises(GatewayError, match='egress_denied'):
            async with transport.open(SimpleNamespace(root=root), None, account_call()):
                pass
    for _ in range(2):
        async with transport.open(SimpleNamespace(root='https://chatgpt.com/backend-api/wham'), None, account_call()) as response:
            assert response.status == 302
    assert len(requests) == 2
    assert all('cookie' not in r.headers for r in requests)
    account_call(method='POST', suffix='rate-limit-reset-credits/consume', body={'redeem_request_id': str(uuid4())})


@pytest.mark.asyncio
async def test_account_transport_revalidates_mutable_body_and_egress():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    async def private(host, port):
        return ['127.0.0.1']
    calls = []
    transport = Transport(EgressPolicy(resolver=private), adapter=httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(GatewayError, match='egress_denied'):
        async with transport.open(SimpleNamespace(root='https://chatgpt.com/backend-api/wham'), None, account_call()):
            pass
    body = {'redeem_request_id': str(uuid4())}
    call = account_call(method='POST', suffix='rate-limit-reset-credits/consume', body=body)
    body['untrusted'] = 'payload'
    transport = Transport(await policy(), adapter=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(GatewayError, match='egress_denied'):
        async with transport.open(SimpleNamespace(root='https://chatgpt.com/backend-api/wham'), None, call):
            pass
    assert not calls
