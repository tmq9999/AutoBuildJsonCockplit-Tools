from urllib.parse import unquote, urlsplit

from .errors import FlowError
from .models import ProxyConfig

SCHEMES = {"http", "https", "socks5", "socks5h"}


def parse_proxies(text: str) -> list[ProxyConfig]:
    if len(text) > 5 * 1024 * 1024 or len(text.splitlines()) > 10_000:
        raise FlowError("INVALID_INPUT", "proxy_parse")
    result, seen = [], set()
    for line in text.removeprefix("\ufeff").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if "://" in line:
                p = urlsplit(line)
                if p.path or p.query or p.fragment or p.scheme not in SCHEMES or p.port is None:
                    raise ValueError()
                scheme, host, port = p.scheme, p.hostname, p.port
                username = unquote(p.username) if p.username is not None else None
                password = unquote(p.password) if p.password is not None else None
            else:
                parts = line.split("|") if "|" in line else line.split(":")
                scheme = parts.pop(0).lower() if parts[0].lower() in SCHEMES else "http"
                if len(parts) not in {2, 4}:
                    raise ValueError()
                host, port = parts[:2]
                username, password = parts[2:] if len(parts) == 4 else (None, None)
                port = int(port)
            if not host or any(c.isspace() or c in "/@?#\\" for c in host) or not 1 <= port <= 65535:
                raise ValueError()
            if username == "" or (username is not None and password is None):
                raise ValueError()
            if username and any(ord(c) < 32 for c in username + (password or "")):
                raise ValueError()
            address = f"[{host}]" if ":" in host else host.lower()
            proxy = ProxyConfig(f"{scheme}://{address}:{port}", username, password)
        except (ValueError, TypeError):
            raise FlowError("CONFIGURATION_ERROR", "proxy_parse") from None
        if proxy.key not in seen:
            result.append(proxy)
            seen.add(proxy.key)
    return result
