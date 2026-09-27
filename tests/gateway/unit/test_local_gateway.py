def test_local_gateway_preserves_inference_capacity_setting():
    from scripts.local_gateway import preserve_gateway_capacity

    environment = {"PATH": "/usr/bin"}
    preserve_gateway_capacity(environment, {"AUTOBUILD_GATEWAY_INFERENCE_CAPACITY": "17"})
    assert environment["AUTOBUILD_GATEWAY_INFERENCE_CAPACITY"] == "17"


def test_local_gateway_does_not_invent_capacity_setting():
    environment = {"PATH": "/usr/bin"}
    from scripts.local_gateway import preserve_gateway_capacity

    preserve_gateway_capacity(environment, {})
    assert "AUTOBUILD_GATEWAY_INFERENCE_CAPACITY" not in environment
