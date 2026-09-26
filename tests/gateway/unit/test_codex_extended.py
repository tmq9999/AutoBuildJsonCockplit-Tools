import pytest


def test_codex_reference_catalog_exposes_client_metadata_without_entitling_accounts():
    from autobuild_json.gateway.providers.codex_model_catalog import codex_model_catalog, client_model
    models = codex_model_catalog()
    assert {item["id"] for item in models} >= {"gpt-6-astra", "gpt-5.5", "gpt-image-2.5"}
    astra = client_model("gpt-6-astra")
    assert astra["context_window"] == 256000
    assert astra["auto_compact_token_limit"] == 230400
    # The live endpoint rejects ultra with invalid_value; max is the highest
    # supported effort and must be advertised without silent downgrade.
    assert astra["supported_reasoning_levels"][-1]["effort"] == "max"


def test_images_codec_translates_generation_and_edit_inputs():
    from autobuild_json.gateway.protocols.openai_images import ImagesCodec
    generation = ImagesCodec().decode({"prompt": "a blue bird", "model": "gpt-image-2.5"})
    assert "images" in generation.required_capabilities and generation.image_tool.action == "generate"
    edit = ImagesCodec(edit=True).decode({"prompt": "make it red", "image": "data:image/png;base64,UE5H"})
    assert {"images", "vision"} <= edit.required_capabilities
    assert edit.image_tool.action == "edit"


@pytest.mark.asyncio
async def test_compact_events_keep_opaque_encrypted_output_and_usage():
    from autobuild_json.gateway.providers.responses_events import compact_events
    class Response:
        async def read_json(self, max_bytes):
            return {"object": "response.compaction", "id": "r", "output": [
                {"type": "compaction", "id": "c", "encrypted_content": "opaque"}],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}
    events = [event async for event in compact_events(Response())]
    assert [event.kind for event in events] == ["started", "block_started", "block_finished", "finished"]
    assert events[-1].usage.input_tokens == 4
