import pytest
from anthropic import AsyncAnthropic

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_anthropic_sdk_json_and_stream(sdk_server):
    async with AsyncAnthropic(base_url=sdk_server.base_url, api_key=sdk_server.secret, max_retries=0) as client:
        result = await client.messages.create(model="public", max_tokens=8, messages=[{"role": "user", "content": "hi"}])
        assert result.content[0].text == "Hello"
        async with client.messages.stream(model="public", max_tokens=8, messages=[{"role": "user", "content": "hi"}]) as stream:
            assert "".join([chunk async for chunk in stream.text_stream]) == "Hello"
            final = await stream.get_final_message()
            assert final.usage.output_tokens == 5
