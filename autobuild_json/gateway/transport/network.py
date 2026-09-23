import httpcore
from httpcore._backends.auto import AutoBackend


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy, backend=None):
        self.policy = policy
        self.backend = backend or AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = await self.policy.addresses(host, port)
        # Only the approved literal is handed to the socket connector. TLS SNI is
        # negotiated by httpcore with the original request host, not this literal.
        return await self.backend.connect_tcp(host=addresses[0], port=port, timeout=timeout,
                                              local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise ValueError("egress_denied")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)
