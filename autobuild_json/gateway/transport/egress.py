import asyncio
from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import unquote, urlsplit


def validate_addresses(addresses, allowed_networks=()):
    if not addresses:
        raise ValueError("egress_denied")
    networks = [ipaddress.ip_network(item) for item in allowed_networks]
    try:
        for raw in addresses:
            ip = ipaddress.ip_address(raw)
            ip = getattr(ip, "ipv4_mapped", None) or ip
            permitted = any(ip.version == network.version and ip in network for network in networks)
            if not ip.is_global and not permitted or ip.is_multicast or ip.is_unspecified:
                raise ValueError()
    except ValueError:
        raise ValueError("egress_denied") from None


async def resolve(host, port):
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(row[4][0] for row in rows))


@dataclass(frozen=True)
class ResolvedTarget:
    host: str
    port: int
    approved_addresses: tuple[str, ...]
    tls_name: str


class EgressPolicy:
    def __init__(self, *, resolver=resolve, allowed_networks=(), private_origins=(), trusted_proxy_origins=()):
        self.resolver = resolver
        self.allowed_networks = tuple(allowed_networks)
        self.private_origins = frozenset(private_origins)
        self.trusted_proxy_origins = frozenset(trusted_proxy_origins)

    async def addresses(self, host, port):
        addresses = await self.resolver(host, port)
        permitted_hosts = set()
        for origin in self.private_origins | self.trusted_proxy_origins:
            parsed = urlsplit(origin)
            permitted_hosts.add((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)))
        networks = self.allowed_networks if (host, port) in permitted_hosts else ()
        validate_addresses(addresses, networks)
        return tuple(addresses)

    async def resolve_and_check(self, root):
        try:
            p = urlsplit(root)
            if (p.scheme not in {"http", "https"} or not p.hostname or p.username is not None
                    or p.query or p.fragment or "\\" in root or any(ord(c) <= 32 for c in root)
                    or any(segment in {".", ".."} for segment in unquote(p.path).split("/"))):
                raise ValueError()
            origin = f"{p.scheme}://{p.netloc}"
            if p.scheme == "http" and origin not in self.private_origins:
                raise ValueError()
            port = p.port or (443 if p.scheme == "https" else 80)
            addresses = await self.addresses(p.hostname, port)
            if any(not ipaddress.ip_address(address).is_global for address in addresses) and origin not in self.private_origins:
                raise ValueError()
            return ResolvedTarget(p.hostname, port, addresses, p.hostname)
        except (ValueError, OSError):
            raise ValueError("egress_denied") from None
