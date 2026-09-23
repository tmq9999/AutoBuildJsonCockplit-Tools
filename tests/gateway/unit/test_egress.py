import pytest


@pytest.mark.parametrize("addresses", [["93.184.216.34", "169.254.169.254"], ["::ffff:127.0.0.1"], ["::1"], [], ["0.0.0.0"]])
def test_unsafe_dns_answer_rejected(addresses):
    from autobuild_json.gateway.transport.egress import validate_addresses
    with pytest.raises(ValueError, match="egress_denied"):
        validate_addresses(addresses, ())


@pytest.mark.asyncio
async def test_connect_resolves_again_and_rejects_rebinding():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.network import GuardedBackend
    calls = 0
    async def resolver(host, port):
        nonlocal calls
        calls += 1
        return ["93.184.216.34"] if calls == 1 else ["127.0.0.1"]
    policy = EgressPolicy(resolver=resolver)
    await policy.resolve_and_check("https://provider.invalid/v1")
    backend = GuardedBackend(policy)
    with pytest.raises(ValueError, match="egress_denied"):
        await backend.connect_tcp("provider.invalid", 443)


@pytest.mark.asyncio
async def test_connect_pins_approved_address_instead_of_reresolving():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    from autobuild_json.gateway.transport.network import GuardedBackend
    async def resolver(host, port):
        return ["93.184.216.34"]
    received = []
    marker = object()
    class Backend:
        async def connect_tcp(self, **kwargs):
            received.append(kwargs)
            return marker
    result = await GuardedBackend(EgressPolicy(resolver=resolver), Backend()).connect_tcp("provider.invalid", 443)
    assert result is marker
    assert received[0]["host"] == "93.184.216.34" and received[0]["port"] == 443


@pytest.mark.asyncio
async def test_plain_http_requires_exact_private_origin_and_network_allowance():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    async def resolver(host, port):
        return ["127.0.0.1"]
    policy = EgressPolicy(resolver=resolver, allowed_networks=("127.0.0.0/8",),
                          private_origins={"http://localhost:11434"})
    assert (await policy.resolve_and_check("http://localhost:11434/api")).host == "localhost"
    with pytest.raises(ValueError):
        await policy.resolve_and_check("http://localhost:9999/api")


@pytest.mark.asyncio
async def test_allowing_private_network_does_not_allow_every_private_origin():
    from autobuild_json.gateway.transport.egress import EgressPolicy
    async def resolver(host, port):
        return ["127.0.0.1"]
    policy = EgressPolicy(resolver=resolver, allowed_networks=("127.0.0.0/8",),
                          private_origins={"http://localhost:11434"})
    with pytest.raises(ValueError, match="egress_denied"):
        await policy.resolve_and_check("https://not-authorized.invalid/api")
