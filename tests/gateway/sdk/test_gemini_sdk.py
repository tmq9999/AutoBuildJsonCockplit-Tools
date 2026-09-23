import pytest
from google import genai
from google.genai import types

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_google_genai_sdk_json_and_stream(sdk_server):
    client = genai.Client(api_key=sdk_server.secret, http_options=types.HttpOptions(base_url=sdk_server.base_url, api_version="v1beta"))
    try:
        result = await client.aio.models.generate_content(model="public", contents="hi", config=types.GenerateContentConfig(max_output_tokens=8))
        assert result.text == "Hello"
        stream = await client.aio.models.generate_content_stream(model="public", contents="hi")
        assert "".join([chunk.text or "" async for chunk in stream]) == "Hello"
    finally:
        await client.aio.aclose()
        client.close()
