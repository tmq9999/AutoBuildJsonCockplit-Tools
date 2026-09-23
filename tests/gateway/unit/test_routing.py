import pytest
from pydantic import ValidationError


def test_alias_never_grants_hidden_model():
    from autobuild_json.gateway.routing.select import eligible
    assert not eligible("private", {"public"}, {"tools"}, {"text", "tools"}, True)
    assert not eligible("public", {"public"}, {"tools"}, {"text"}, True)
    assert not eligible("public", {"public"}, {"text"}, {"text"}, False)
    assert eligible("public", {"public"}, {"text"}, {"text"}, True)


@pytest.mark.parametrize("root", ["https://user:secret@example.com/v1", "https://example.com/v1?key=x",
                                  "file:///tmp", "http://example.com/v1#anchor", "https://example.com/../admin"])
def test_provider_root_forbids_embedded_credentials_and_dynamic_targets(root):
    from autobuild_json.gateway.routing.records import ProviderConfig
    with pytest.raises(ValidationError):
        ProviderConfig(name="x", adapter="openai_compatible", root=root)
