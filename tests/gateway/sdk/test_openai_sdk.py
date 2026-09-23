import pytest
from openai import AsyncOpenAI

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_openai_sdk_chat_json_and_stream(sdk_server):
    async with AsyncOpenAI(base_url=sdk_server.base_url+"/v1", api_key=sdk_server.secret, max_retries=0) as client:
        result = await client.chat.completions.create(model="public", messages=[{"role": "user", "content": "hi"}], max_tokens=8)
        assert result.choices[0].message.content == "Hello"
        stream = await client.chat.completions.create(model="public", messages=[], max_tokens=8, stream=True, stream_options={"include_usage": True})
        chunks = [chunk async for chunk in stream]
        assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello"
        assert chunks[-1].usage.prompt_tokens == 4


async def test_openai_sdk_responses_json_and_stream(sdk_server):
    async with AsyncOpenAI(base_url=sdk_server.base_url+"/v1", api_key=sdk_server.secret, max_retries=0) as client:
        result = await client.responses.create(model="public", input="hi", max_output_tokens=8, store=False)
        assert result.output_text == "Hello"
        stream = await client.responses.create(model="public", input="hi", stream=True, store=False)
        events = [event async for event in stream]
        assert events[-1].type == "response.completed"
