"""Request-local pool selection and affinity; no lock survives these calls."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

from ..accounts.proxy_selection import AccountProxyResolver
from ..errors import GatewayError
from .account_pool import order_candidates
from .continuations import ContinuationScope
from .pool_records import PoolScope
from .pool_store import PoolStore


def session_digest(key, principal, model_id, value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or any(not 32 <= ord(c) <= 126 for c in value)
            or not isinstance(key, bytes) or len(key) < 32):
        raise GatewayError("invalid_request")
    payload = json.dumps([str(principal.customer_id), str(principal.key_id), model_id, value],
                         separators=(",", ":")).encode()
    return hmac.digest(key, b"gateway-session-v1\x00" + payload, hashlib.sha256)


@dataclass(frozen=True)
class Selection:
    request: object
    routes: tuple
    scope: ContinuationScope
    pinned: bool
    retry_limit: int


async def select(engine, principal, request, meta):
    model_id = await engine.catalog.resolve_model(principal, request.model)
    request = request.model_copy(update={"model": model_id})
    scope = ContinuationScope(principal.customer_id, principal.key_id, model_id)
    native = None
    if request.continuation:
        native = await engine.continuations.resolve(request.continuation, scope)
        candidates = await engine.catalog.account_candidates(principal, request, binding_id=native.route_id, canonical_model=True)
        candidates = tuple(c for c in candidates if c.route.credential_id == native.credential_id)
        request = request.model_copy(update={"continuation": native.upstream_id})
    else:
        candidates = await engine.catalog.account_candidates(principal, request, canonical_model=True)
    policy = await engine.catalog.pool_policy(model_id)
    routes = order_candidates(policy, candidates, datetime.now(timezone.utc))
    if not routes:
        raise GatewayError("invalid_state" if native else "upstream_unavailable", 400 if native else 503)
    pinned = native is not None
    if meta.session_digest is not None and (policy is None or policy.session_affinity):
        if not isinstance(engine.digest_key, bytes) or len(engine.digest_key) < 32:
            raise GatewayError("invalid_request")
        pools = PoolStore(engine.db, scope_key=hmac.digest(engine.digest_key, b"gateway-pool-scope-v1", hashlib.sha256))
        affinity = PoolScope(customer_id=principal.customer_id, key_id=principal.key_id,
                             model_id=model_id, session_digest=meta.session_digest)
        winner = await pools.resolve(affinity)
        if winner is None and routes[0].adapter == "codex_oauth" and (policy is None or policy.session_affinity):
            winner = await pools.bind(affinity, routes[0], datetime.now(timezone.utc)+timedelta(hours=24))
        if winner is not None:
            if native and (winner.binding_id != native.route_id or winner.credential_id != native.credential_id):
                raise GatewayError("invalid_state")
            if not request.required_capabilities <= winner.capabilities:
                raise GatewayError("unsupported_feature")
            routes, pinned = (winner,), True
    return Selection(request, tuple(routes), scope, pinned,
                     policy.retry_limit if policy else (7 if routes[0].adapter == "codex_oauth" else 1))


async def revalidate(engine, principal, request, selected, stamp=None):
    if stamp is not None:
        _, current = await AccountProxyResolver(engine.db, engine.proxy_resolver).policy(selected.credential_id)
        if stamp != current:
            raise GatewayError("invalid_state")
    live = await engine.catalog.revalidate(principal, request, selected)
    # Customer rates are frozen by Ledger.reserve, not refreshed by a retry.
    live = replace(live, input_micro=selected.input_micro, output_micro=selected.output_micro,
                   cache_read_micro=selected.cache_read_micro, cache_write_micro=selected.cache_write_micro)
    if live != selected:
        raise GatewayError("invalid_state")
    if selected.adapter == "codex_oauth":
        policy = await engine.catalog.pool_policy(selected.public_model_id)
        if policy is not None and not order_candidates(policy, await engine.catalog._account_observations((live,)),
                                                       datetime.now(timezone.utc)):
            raise GatewayError("invalid_state")
