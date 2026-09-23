import pytest
from ollama import AsyncClient

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_ollama_sdk_json_and_stream(sdk_server):
    client = AsyncClient(host=sdk_server.base_url, headers={"Authorization": "Bearer "+sdk_server.secret})
    try:
        result = await client.chat(model="public", messages=[{"role": "user", "content": "hi"}])
        assert result.message.content == "Hello"
        stream = await client.generate(model="public", prompt="hi", stream=True)
        chunks = [chunk async for chunk in stream]
        assert "".join(chunk.response for chunk in chunks) == "Hello"
        assert chunks[-1].done
    finally:
        await client._client.aclose()
