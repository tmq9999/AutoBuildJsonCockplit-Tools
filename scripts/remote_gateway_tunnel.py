#!/usr/bin/env python3
"""
Remote Cloudflare Tunnel launcher for AutoBuild Public Gateway API (port 8788).

Bridges external HTTPS requests from Cloudflare Tunnel to the
strict loopback boundary of the public gateway API (127.0.0.1:8788).
Supports full streaming (Server-Sent Events / SSE) and standard JSON requests.
"""
import asyncio
import os
import re
import sys
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import StreamingResponse

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8788")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8785"))
CLOUDFLARED_BIN = os.environ.get(
    "CLOUDFLARED_BIN",
    "/home/mquangprovip0503/.9router/bin/cloudflared"
    if os.path.exists("/home/mquangprovip0503/.9router/bin/cloudflared")
    else "/home/mquangprovip0503/.9remote/bin/cloudflared"
)

client = httpx.AsyncClient(
    base_url=GATEWAY_URL,
    timeout=httpx.Timeout(300.0, connect=10.0),
    follow_redirects=False,
)

async def proxy_request(request: Request):
    headers = dict(request.headers)
    headers["host"] = "127.0.0.1:8788"
    if "origin" in headers:
        headers["origin"] = "http://127.0.0.1:8788"

    url_path = request.url.path
    if request.url.query:
        url_path += f"?{request.url.query}"

    req_body = await request.body()
    try:
        out_req = client.build_request(
            method=request.method,
            url=url_path,
            headers=headers,
            content=req_body,
        )
        r = await client.send(out_req, stream=True)
    except httpx.RequestError as exc:
        from starlette.responses import Response
        return Response(f"Gateway connection error: {exc}", status_code=502)

    async def body_stream():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()

    resp_headers = dict(r.headers)
    resp_headers.pop("content-length", None)
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("transfer-encoding", None)

    return StreamingResponse(
        body_stream(),
        status_code=r.status_code,
        headers=resp_headers,
    )

app = Starlette(
    routes=[
        Route("/", endpoint=proxy_request, methods=["GET", "POST", "HEAD", "OPTIONS"]),
        Route("/{path:path}", endpoint=proxy_request, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    ]
)

async def run_tunnel():
    config = uvicorn.Config(app, host="127.0.0.1", port=BRIDGE_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)

    print(f"[*] Loopback bridge running on http://127.0.0.1:{BRIDGE_PORT} -> {GATEWAY_URL}", flush=True)

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
                print("\n" + "=" * 70, flush=True)
                print("[+] CLOUDFLARE TUNNEL CHO GATEWAY API (PORT 8788) SẴN SÀNG:", flush=True)
                print(f"    Public Base URL:      {tunnel_url}/v1", flush=True)
                print(f"    OpenAI Chat URL:      {tunnel_url}/v1/chat/completions", flush=True)
                print(f"    OpenAI Responses URL: {tunnel_url}/v1/responses", flush=True)
                print(f"    Anthropic URL:        {tunnel_url}/v1/messages", flush=True)
                print(f"    Health check:         {tunnel_url}/health", flush=True)
                print("=" * 70 + "\n", flush=True)
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
