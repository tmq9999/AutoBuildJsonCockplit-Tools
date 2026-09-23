from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError
from ..routing.records import ProviderConfig
from ..secrets import Ciphertext
from ..transport.http import OutboundRequest
from ..proxy.config import ProxySelection


async def discover(services, provider_id):
    async with services.db.sessions() as session:
        row = (
            await session.execute(text("SELECT config FROM providers WHERE id=:id"), {"id": provider_id})
        ).scalar_one_or_none()
        cred = (
            (
                await session.execute(
                    text(
                        "SELECT id,encrypted_secret FROM credentials WHERE provider_id=:id AND enabled ORDER BY id LIMIT 1"
                    ),
                    {"id": provider_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None or cred is None:
        raise GatewayError("not_found", 404)
    config = ProviderConfig.model_validate(row)
    if config.adapter == "codex_oauth" or not config.enabled:
        raise GatewayError("unsupported_feature")
    secret = services.vault.open(
        "credential", cred["id"], Ciphertext.from_dict(cred["encrypted_secret"])
    ).decode()
    auth = {
        "bearer": ("Authorization", "Bearer " + secret),
        "x-api-key": ("x-api-key", secret),
        "x-goog-api-key": ("x-goog-api-key", secret),
        "none": None,
    }.get(config.auth_mode)
    headers = (("anthropic-version", "2023-06-01"),) if config.adapter == "anthropic" else ()
    selection = (
        await services.profiles.load(config.proxy_profile_id)
        if config.proxy_profile_id
        else ProxySelection("direct")
    )
    async with services.proxies.acquire(
        selection, uuid4(), datetime.now(timezone.utc) + timedelta(seconds=30)
    ) as lease:
        route = SimpleNamespace(root=config.root, proxy_profile_id=config.proxy_profile_id)
        call = OutboundRequest(
            "GET",
            "tags" if config.adapter == "ollama" else "models",
            None,
            auth_header=auth,
            headers=headers,
            deadline=time.monotonic() + 30,
        )
        async with services.transport.open(route, lease.proxy, call) as response:
            if response.status != 200:
                raise GatewayError("upstream_error", 502)
            payload = await response.read_json()
    if not isinstance(payload, dict):
        raise GatewayError("upstream_error", 502)
    values = payload.get("data", payload.get("models"))
    if not isinstance(values, list) or len(values) > 10000:
        raise GatewayError("upstream_error", 502)
    models = []
    for item in values:
        name = item.get("id", item.get("name")) if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 200
            or any(ord(c) < 32 for c in name)
            or secret
            and secret in name
        ):
            raise GatewayError("upstream_error", 502)
        models.append(name.removeprefix("models/") if config.adapter == "gemini" else name)
    await services.catalog.record_discovery(provider_id, models)
    return {"models": sorted(set(models)), "published": False}


async def playground(services, payload):
    from ..protocols.openai_chat import OpenAIChatCodec
    from ..protocols.openai_responses import ResponsesCodec
    from ..protocols.anthropic import AnthropicCodec
    from ..protocols.gemini import GeminiCodec
    from ..protocols.ollama import OllamaCodec

    principal = await services.identity.authenticate(payload.client_key.get_secret_value(), payload.protocol)
    body = payload.body
    codecs = {
        "openai": ResponsesCodec if "input" in body else OpenAIChatCodec,
        "anthropic": AnthropicCodec,
        "gemini": GeminiCodec,
        "ollama": OllamaCodec,
    }
    codec = codecs[payload.protocol]()
    options = {}
    if payload.protocol == "gemini":
        body = dict(body)
        options = {"model": body.pop("model", ""), "stream": False}
    if body.get("stream") is True:
        raise GatewayError("unsupported_feature")
    if payload.protocol == "ollama":
        body = dict(body, stream=False)
    decoded = codec.decode(body, options)
    prepared = await services.engine.prepare(
        principal, decoded, services.engine.meta(body, protocol=payload.protocol)
    )
    return codec.encode_result(await prepared.collect())
