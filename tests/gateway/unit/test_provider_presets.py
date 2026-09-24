from copy import deepcopy

from autobuild_json.gateway.routing.records import ProviderConfig


def test_all_presets_build_provider_configs_when_admin_supplies_root():
    from autobuild_json.gateway.providers.presets import list_presets

    presets = list_presets()
    assert {item["id"] for item in presets} == {
        "custom-openai-chat", "custom-openai-responses", "anthropic-native", "gemini-native", "ollama-native",
    }
    assert {item["version"] for item in presets} == {1}
    for item in presets:
        config = deepcopy(item["provider"])
        assert config.pop("root") == ""
        provider = ProviderConfig(**config, root="https://example.test/v1")
        assert provider.cost_schedule is None
        assert provider.budget_id is None
        assert provider.proxy_profile_id is None
    assert [item["provider"]["adapter"] for item in presets] == [
        "openai_compatible", "openai_compatible", "anthropic", "gemini", "ollama",
    ]
    assert [item["provider"]["wire_api"] for item in presets[:2]] == ["chat", "responses"]


def test_preset_listing_returns_independent_data_without_hidden_privileges():
    from autobuild_json.gateway.providers.presets import list_presets

    first = list_presets()
    assert not any("secret" in repr(item).lower() or "model_id" in repr(item).lower() for item in first)
    assert not any("tools" in repr(item["provider"]).lower() for item in first)
    first[0]["provider"]["name"] = "changed"
    first[0]["id"] = "changed"
    second = list_presets()
    assert second[0]["id"] == "custom-openai-chat"
    assert second[0]["provider"]["name"] != "changed"
