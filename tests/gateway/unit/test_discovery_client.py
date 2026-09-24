import gzip
import time

import httpx
import pytest

from autobuild_json.gateway.errors import GatewayError
from autobuild_json.gateway.transport.http import UpstreamResponse


@pytest.mark.asyncio
async def test_reader_counts_decoded_bytes_and_rejects_expansion():
    from autobuild_json.gateway.transport.discovery import read_discovery_json
    raw = httpx.Response(200, content=gzip.compress(b'{"data":"' + b'x' * 128 + b'"}'),
                         headers={"content-encoding": "gzip"})
    with pytest.raises(GatewayError, match="catalog_limit_exceeded"):
        await read_discovery_json(UpstreamResponse(raw, time.monotonic()+5), 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}', b'{"a":NaN}', b'[' * 33 + b'0' + b']' * 33])
async def test_reader_rejects_ambiguous_or_deep_json(payload):
    from autobuild_json.gateway.transport.discovery import read_discovery_json
    with pytest.raises(GatewayError, match="upstream_error"):
        await read_discovery_json(UpstreamResponse(httpx.Response(200, content=payload), time.monotonic()+5), 2_097_152)


@pytest.mark.asyncio
async def test_reader_returns_decoded_size():
    from autobuild_json.gateway.transport.discovery import read_discovery_json
    body = b'{"models":[]}'
    value, size = await read_discovery_json(UpstreamResponse(httpx.Response(200, content=body), time.monotonic()+5), 100)
    assert value == {"models": []}
    assert size == len(body)
