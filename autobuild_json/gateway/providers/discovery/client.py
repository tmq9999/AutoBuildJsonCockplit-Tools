"""Bounded catalog reads through one sticky proxy lease."""

import asyncio
from datetime import datetime, timedelta, timezone
import time

from ...catalog.digests import content_digest
from ...errors import GatewayError, UpstreamRejected
from ...transport.discovery import TOTAL_BYTES, discovery_request, read_discovery_json
from ..preflight import auth_header
from ..retry import parse_retry_after
from .pagination import MAX_IDS, MAX_PAGES
from .parsers import parse_page


class DiscoveryClient:
    def __init__(self, transport, limits, credential_resolver):
        self.transport = transport
        self.limits = limits
        self.credential_resolver = credential_resolver

    async def _auth(self, context, deadline):
        if context.route.auth_mode == "none":
            return None, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "upstream")
        try:
            secret = await asyncio.wait_for(self.credential_resolver(context.route), remaining)
        except asyncio.TimeoutError:
            raise GatewayError("deadline_exceeded", 504, "upstream") from None
        return auth_header(context.route.auth_mode, secret), secret

    async def _page(self, context, lease, check_current, deadline, cursor, remaining_bytes, auth, secret):
        await check_current()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "upstream")
        admission_deadline = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        request = discovery_request(context.source.mode.value, cursor, auth, deadline)
        async with self.limits.acquire(context.route, admission_deadline):
            async with self.transport.open(context.route, lease.proxy, request) as response:
                if response.status == 429:
                    rejection = UpstreamRejected(429, parse_retry_after(response.headers.get("retry-after")))
                    # Capture before response/admission/proxy cleanup; those may
                    # block and must not restart Retry-After when they finish.
                    rejection.received_at = time.monotonic()
                    raise rejection
                if response.status != 200:
                    raise UpstreamRejected(response.status)
                payload, used = await read_discovery_json(response, remaining_bytes)
        page = parse_page(context.source.mode.value, payload, datetime.now(timezone.utc), secret)
        return page, used

    async def _bounded_page(self, context, lease, check_current, deadline, cursor, remaining_bytes, auth, secret):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError("deadline_exceeded", 504, "upstream")
        page = asyncio.create_task(self._page(context, lease, check_current, deadline, cursor,
                                             remaining_bytes, auth, secret))
        try:
            return await asyncio.wait_for(asyncio.shield(page), remaining)
        except asyncio.TimeoutError:
            raise GatewayError("deadline_exceeded", 504, "upstream") from None
        finally:
            if not page.done():
                page.cancel()
                async def join():
                    try:
                        await asyncio.wait_for(page, 5)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                cleanup = asyncio.create_task(join())
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                await cleanup
                if cancelled:
                    raise asyncio.CancelledError

    async def collect(self, context, lease, check_current, deadline, *, on_warnings=None):
        auth, secret = await self._auth(context, deadline)
        collected = {}
        cursor = None
        seen_cursors = set()
        total_bytes = 0
        observed_at = datetime.now(timezone.utc)
        warnings = set()
        for page_number in range(MAX_PAGES):
            page, used = await self._bounded_page(context, lease, check_current, deadline, cursor,
                                                  TOTAL_BYTES-total_bytes, auth, secret)
            total_bytes += used
            # One timestamp makes identical IDs on different pages identical records.
            entries = tuple(entry.model_copy(update={"metadata": {
                key: value.model_copy(update={"observed_at": observed_at})
                for key, value in entry.metadata.items()
            }}) for entry in page.entries)
            for entry in entries:
                previous = collected.get(entry.upstream_id)
                if previous is not None and content_digest((previous,)) != content_digest((entry,)):
                    raise GatewayError("catalog_conflict", 502, "upstream")
                collected[entry.upstream_id] = entry
                if len(collected) > MAX_IDS:
                    raise GatewayError("catalog_limit_exceeded", 502, "upstream")
            warnings.update(page.warnings)
            if page.complete:
                if on_warnings is not None:
                    on_warnings(tuple(sorted(warnings)))
                return tuple(collected.values())
            if page.next_cursor in seen_cursors or page.next_cursor == cursor:
                raise GatewayError("catalog_incomplete", 502, "upstream")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        raise GatewayError("catalog_limit_exceeded", 502, "upstream")

    async def check(self, context, lease, check_current, deadline):
        start = time.monotonic()
        auth, secret = await self._auth(context, deadline)
        try:
            page, _ = await self._bounded_page(context, lease, check_current, deadline, None,
                                               TOTAL_BYTES, auth, secret)
            status, readable, warnings = 200, True, list(page.warnings)
        except UpstreamRejected as exc:
            if exc.upstream_status == 429:
                raise
            status, readable, warnings = exc.upstream_status, False, []
        return {"reachable": True, "catalog_readable": readable,
                "authentication": "unverified", "status_code": status,
                "duration_ms": min(60000, round((time.monotonic()-start)*1000, 3)),
                "warnings": warnings}
