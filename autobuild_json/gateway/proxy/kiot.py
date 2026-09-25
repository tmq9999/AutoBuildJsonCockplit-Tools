import asyncio
from dataclasses import dataclass
import json
import math
import time

import httpcore
import httpx

from ...proxies import parse_proxies
from ...errors import FlowError
from ..errors import GatewayError
from ..transport.egress import EgressPolicy, validate_addresses
from ..transport.http import CoreTransport

ROOT = "https://api.kiotproxy.com/api/v1/proxies"


def lease_action(active, ttl, cooldown, required, rotate):
    if active:
        return "busy"
    if ttl >= required+15 and (not rotate or cooldown > 0):
        return "reuse"
    return "wait" if cooldown > 0 else "new"


@dataclass(frozen=True)
class KiotLease:
    proxy: object
    expires_at: float
    next_at: float

    @property
    def ttl(self):
        return max(0, self.expires_at-time.monotonic())

    @property
    def cooldown(self):
        return max(0, self.next_at-time.monotonic())


def parse_lease(payload, protocol, received_at):
    try:
        data, timestamp = payload["data"], payload["timestamp"]
        times = [data[name] for name in ("ttl", "ttc", "expirationAt", "nextRequestAt")] + [timestamp]
        if any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in times):
            raise ValueError()
        ttl = min(data["ttl"], (data["expirationAt"]-timestamp)/1000)
        cooldown = max(data["ttc"], (data["nextRequestAt"]-timestamp)/1000, 0)
        if ttl <= 0 or protocol not in {"http", "socks5"}:
            raise ValueError()
        proxy = parse_proxies(protocol+"://"+data[protocol])[0]
        from urllib.parse import urlsplit
        validate_addresses([urlsplit(proxy.server).hostname])
        if proxy.username is not None:
            raise ValueError()
        return KiotLease(proxy, received_at+ttl, received_at+cooldown)
    except (ValueError, KeyError, TypeError, IndexError, FlowError):
        raise ValueError("invalid_kiot_response") from None


class KiotClient:
    def __init__(self, *, adapter=None):
        self.adapter = adapter

    async def _call(self, operation, key, deadline, region=None):
        if operation not in {"current", "new", "out"}:
            raise GatewayError("invalid_request")
        params = {"key": key}
        if region:
            params["region"] = region
        # Calling the transport directly avoids HTTPX's request URL INFO logging.
        transport = self.adapter or CoreTransport(EgressPolicy(), None)
        request = httpx.Request("GET", ROOT+"/"+operation, params=params,
                               extensions={"timeout": {"connect": 5, "read": 5, "write": 5, "pool": 5}})
        async def exchange():
            response = await transport.handle_async_request(request)
            try:
                if response.status_code == 429:
                    raise GatewayError("proxy_not_ready", 503, "proxy")
                # Kiot uses HTTP 400 for recognized business errors (including
                # no current allocation). Other statuses must not authorize /new.
                if response.status_code not in {200, 400}:
                    raise GatewayError("kiot_unavailable", 503, "proxy")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data)+len(chunk) > 65536:
                        raise GatewayError("kiot_unavailable", 503, "proxy")
                    data.extend(chunk)
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    raise GatewayError("kiot_unavailable", 503, "proxy") from None
                if not isinstance(payload, dict):
                    raise GatewayError("kiot_unavailable", 503, "proxy")
                return response.status_code, payload
            finally:
                await response.aclose()
        try:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            status, payload = await asyncio.wait_for(exchange(), min(10, remaining))
            if status == 400:
                if payload.get("success") is not False:
                    raise GatewayError("kiot_unavailable", 503, "proxy")
                if payload.get("error") == "PROXY_NOT_FOUND_BY_KEY" and operation == "current":
                    return None
                if payload.get("error") == "KEY_NOT_FOUND":
                    raise GatewayError("kiot_key_invalid", 400, "proxy")
                raise GatewayError("kiot_unavailable", 503, "proxy")
            if status != 200:
                raise GatewayError("kiot_unavailable", 503, "proxy")
            if payload.get("success") is not True:
                if payload.get("error") == "PROXY_NOT_FOUND_BY_KEY" and operation == "current":
                    return None
                if payload.get("error") == "KEY_NOT_FOUND":
                    raise GatewayError("kiot_key_invalid", 400, "proxy")
                raise GatewayError("kiot_unavailable", 503, "proxy")
            return payload
        except GatewayError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
            raise GatewayError("kiot_allocation_uncertain" if operation == "new" else "proxy_not_ready", 503, "proxy") from None
        except (httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError, OSError):
            raise GatewayError("kiot_allocation_uncertain" if operation == "new" else "kiot_unavailable", 503, "proxy") from None
        except ValueError:
            raise GatewayError("kiot_unavailable", 503, "proxy") from None
        finally:
            await transport.aclose()

    async def current(self, key, *, protocol, deadline):
        result = await self._call("current", key, deadline)
        if result is None:
            return None
        try:
            return parse_lease(result, protocol, time.monotonic())
        except ValueError:
            raise GatewayError("kiot_unavailable", 503, "proxy") from None

    async def new(self, key, *, protocol, region, deadline):
        if region not in {"random", "bac", "trung", "nam"}:
            raise GatewayError("invalid_request")
        result = await self._call("new", key, deadline, region)
        try:
            return parse_lease(result, protocol, time.monotonic())
        except ValueError:
            raise GatewayError("kiot_unavailable", 503, "proxy") from None

    async def out(self, key, *, deadline):
        result = await self._call("out", key, deadline)
        if result.get("data") is not True:
            raise GatewayError("kiot_unavailable", 503, "proxy")
