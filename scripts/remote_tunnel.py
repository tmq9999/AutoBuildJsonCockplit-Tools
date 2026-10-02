#!/usr/bin/env python3
"""
Remote Cloudflare Tunnel launcher for AutoBuild Admin Console (port 8787).

Bridges external HTTPS requests from Cloudflare Tunnel to the
strict loopback boundary of the private admin server (127.0.0.1:8787).
"""
import asyncio
import os
import re
import signal
import sys
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response

ADMIN_URL = os.environ.get("ADMIN_URL", "http://127.0.0.1:8787")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8786"))
CLOUDFLARED_BIN = os.environ.get(
    "CLOUDFLARED_BIN",
    "/home/mquangprovip0503/.9router/bin/cloudflared"
    if os.path.exists("/home/mquangprovip0503/.9router/bin/cloudflared")
    else "/home/mquangprovip0503/.9remote/bin/cloudflared"
)

client = httpx.AsyncClient(
    base_url=ADMIN_URL,
    timeout=httpx.Timeout(120.0, connect=10.0),
    follow_redirects=False,
)

async def proxy_request(request: Request) -> Response:
    headers = dict(request.headers)
    headers["host"] = "127.0.0.1:8787"
    if "origin" in headers:
        headers["origin"] = "http://127.0.0.1:8787"

    url_path = request.url.path
    if request.url.query:
        url_path += f"?{request.url.query}"

    body = await request.body()
    try:
        resp = await client.request(
            method=request.method,
            url=url_path,
            headers=headers,
            content=body,
        )
    except httpx.RequestError as exc:
        return Response(f"Bridge connection error: {exc}", status_code=502)

    resp_headers = dict(resp.headers)
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    starlette_resp = Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )
    cookie_headers = [
        (b"set-cookie", v.encode("latin1"))
        for k, v in resp.headers.multi_items()
        if k.lower() == "set-cookie"
    ]
    if cookie_headers:
        starlette_resp.raw_headers = [
            h for h in starlette_resp.raw_headers if h[0].lower() != b"set-cookie"
        ] + cookie_headers

    return starlette_resp

bridge_app = Starlette(
    routes=[
        Route("/", endpoint=proxy_request, methods=["GET", "POST", "HEAD", "OPTIONS"]),
        Route("/{path:path}", endpoint=proxy_request, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    ]
)

async def run_tunnel():
    config = uvicorn.Config(bridge_app, host="127.0.0.1", port=BRIDGE_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for server to start
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)

    print(f"[*] Loopback bridge running on http://127.0.0.1:{BRIDGE_PORT} -> {ADMIN_URL}", flush=True)

    if not os.path.exists(CLOUDFLARED_BIN):
        print(f"[!] cloudflared binary not found at {CLOUDFLARED_BIN}", file=sys.stderr)
        return

    cmd = [CLOUDFLARED_BIN, "tunnel", "--url", f"http://127.0.0.1:{BRIDGE_PORT}"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    tunnel_url = None
    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            match = url_pattern.search(text)
            if match and not tunnel_url:
                tunnel_url = match.group(0)
                print("\n" + "=" * 65, flush=True)
                print(f"[+] CLOUDFLARE TUNNEL SẴN SÀNG:", flush=True)
                print(f"    Giao diện Quản trị: {tunnel_url}/service/", flush=True)
                print(f"    OAuth Workbench:    {tunnel_url}/", flush=True)
                print("=" * 65 + "\n", flush=True)
            if "ERR" in text or "error" in text.lower():
                print(text.strip(), file=sys.stderr, flush=True)
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        server.should_exit = True
        await server_task
        await client.aclose()

if __name__ == "__main__":
    try:
        asyncio.run(run_tunnel())
    except KeyboardInterrupt:
        print("\n[*] Tunnel đã dừng.")
