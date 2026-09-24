import asyncio
from uuid import uuid4

from sqlalchemy import text

from ..errors import GatewayError
from ..routing.records import ProviderConfig
from ..catalog.records import OperationRequest, SourceInput


async def discover(services, provider_id, request=None):
    services.build_catalog_worker()
    async with services.db.sessions() as session:
        config = await session.scalar(text("SELECT config FROM providers WHERE id=:id"), {"id": provider_id})
        credential = await session.scalar(text("SELECT id FROM credentials WHERE provider_id=:id AND enabled ORDER BY id LIMIT 1"), {"id": provider_id})
    if config is None or credential is None:
        raise GatewayError("not_found", 404)
    provider = ProviderConfig.model_validate(config)
    mode = {"openai_compatible": "openai_single", "anthropic": "anthropic", "gemini": "gemini", "ollama": "ollama"}.get(provider.adapter)
    if mode is None:
        raise GatewayError("unsupported_feature", 422)
    async def existing():
        async with services.db.sessions() as session:
            return await session.scalar(text("SELECT id FROM catalog_sources WHERE provider_id=:provider AND credential_id=:credential"),
                                        {"provider": provider_id, "credential": credential})
    identity = await existing()
    if identity is None:
        try:
            source = await services.sources.create(SourceInput(provider_id=provider_id, credential_id=credential, mode=mode), "admin")
            identity = source.id
        except GatewayError as exc:
            if exc.code != "version_conflict":
                raise
            identity = await existing()
            if identity is None:
                raise
    context = await services.sources.context(identity)
    command = OperationRequest(id=uuid4(), source_id=identity, kind="discover", expected=context.stamp)
    claim = await services.operations.enqueue_claimed(command, uuid4(), "admin")
    owner = asyncio.create_task(services.operation_runner.execute(claim))

    async def watch_disconnect():
        while True:
            if await request.is_disconnected():
                owner.cancel()
                return
            await asyncio.sleep(.05)

    watcher = asyncio.create_task(watch_disconnect()) if request is not None else None
    try:
        result = await asyncio.shield(owner)
    finally:
        # The same live owner must close its HTTP/proxy contexts. Repeated
        # cancellation may not detach it or launch a replacement operation.
        if not owner.done():
            owner.cancel()
        if watcher is not None:
            watcher.cancel()
        cleanup = asyncio.gather(owner, *([watcher] if watcher is not None else []), return_exceptions=True)
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        await cleanup
    if result.state != "succeeded":
        code = result.error or "deadline_exceeded"
        if code == "rate_limited":
            async with services.db.sessions() as session:
                retry = await session.scalar(text("SELECT retry_after FROM provider_operations WHERE id=:id"), {"id": result.id})
            raise GatewayError(code, 429, "upstream", retry)
        status = 504 if result.state == "running" or code in {"deadline_exceeded", "operation_expired"} else (
            409 if code in {"version_conflict", "catalog_snapshot_stale", "operation_cancelled"} else 502)
        raise GatewayError(code, status, "upstream")
    async with services.db.sessions() as session:
        models = (await session.execute(text("SELECT upstream_id FROM catalog_entries WHERE run_id=:run ORDER BY upstream_id"),
                                        {"run": result.id})).scalars().all()
    return {"models": models, "published": False, "credential_id": str(credential), "run_id": str(result.id)}


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
