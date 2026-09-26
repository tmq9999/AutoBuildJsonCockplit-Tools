"""Authenticated sequential Responses turns with per-turn metering and cleanup."""
import asyncio
import json
import re
import time

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError

from ..errors import GatewayError
from ..protocols.openai_responses import ResponsesCodec
from .auth import client_secret, gateway_session

_STREAM_ID = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
IDLE_TIMEOUT = 300
CONNECTION_TIMEOUT = 1800


def error_event(error, event):
    kind = "server_error" if error.status >= 500 else {
        401: "authentication_error", 403: "permission_error", 429: "rate_limit_error",
    }.get(error.status, "invalid_request_error")
    value = {"type": "error", "status": error.status,
             "error": {"type": kind, "code": error.code, "message": error.code}}
    stream_id = event.get("stream_id") if isinstance(event, dict) else None
    if isinstance(stream_id, str) and _STREAM_ID.fullmatch(stream_id):
        value["stream_id"] = stream_id
    return value


async def serve_turn(websocket, identity, engine, secret, event):
    if not isinstance(event, dict) or event.get("type") != "response.create":
        raise GatewayError("invalid_request")
    if "stream" in event or "generate" in event:
        # Warmup is not generation and needs its own zero-generation ledger.
        # Never silently turn generate:false into a billed generation.
        raise GatewayError("unsupported_feature")
    stream_id = event.get("stream_id")
    if stream_id is not None and (not isinstance(stream_id, str) or not _STREAM_ID.fullmatch(stream_id)):
        raise GatewayError("invalid_request")
    principal = await identity.authenticate(secret, "openai")
    body = {key: value for key, value in event.items() if key not in {"type", "stream_id"}}
    codec = ResponsesCodec()
    decoded = codec.decode(dict(body, stream=True), {}).model_copy(update={"operation": "websocket"})
    meta = engine.meta({"operation": "websocket", "body": body},
        session_digest=await engine.session_digest(principal, decoded.model, gateway_session(websocket)))
    prepared = await engine.prepare(principal, decoded, meta)
    codec.model = prepared.collector.model
    codec.response_id = prepared.collector.id
    events = prepared.events()
    try:
        async for item in events:
            for frame in codec.encode_event(item):
                value = json.loads(frame.decode().split("data: ", 1)[1])
                if stream_id is not None:
                    value["stream_id"] = stream_id
                await asyncio.wait_for(websocket.send_json(value), 30)
    finally:
        try:
            await events.aclose()
        finally:
            await prepared.close()


def mount_websocket(router, identity, engine, allowed_hosts):
    @router.websocket('/v1/responses')
    @router.websocket('/backend-api/codex/responses')
    async def responses_websocket(websocket: WebSocket):
        host = websocket.headers.get("host")
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        if (host not in allowed_hosts or len(websocket.headers.getlist("host")) != 1
                or len(websocket.headers.getlist("origin")) > 1
                or websocket.headers.get("origin") not in {None, scheme+"://"+host}
                or websocket.query_params):
            await websocket.close(code=1008)
            return
        try:
            secret = client_secret(websocket)
            await identity.authenticate(secret, "openai")
            gateway_session(websocket)
        except GatewayError:
            await websocket.close(code=1008)
            return
        except SQLAlchemyError:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        queue = asyncio.Queue(maxsize=1)
        closed = asyncio.Event()
        close_code = 1000
        peer_disconnected = False

        async def reader():
            nonlocal close_code, peer_disconnected
            try:
                while True:
                    message = await asyncio.wait_for(websocket.receive(), IDLE_TIMEOUT)
                    if message["type"] == "websocket.disconnect":
                        peer_disconnected = True
                        return
                    raw = message.get("text")
                    if not isinstance(raw, str) or len(raw.encode()) > 16*1024*1024:
                        close_code = 1009
                        return
                    try:
                        queue.put_nowait(json.loads(raw))
                    except (ValueError, asyncio.QueueFull):
                        close_code = 1008
                        return
            except (WebSocketDisconnect, asyncio.TimeoutError):
                pass
            finally:
                closed.set()

        read_task = asyncio.create_task(reader())
        closed_task = asyncio.create_task(closed.wait())
        active = None
        try:
            ends = time.monotonic()+CONNECTION_TIMEOUT
            while not closed.is_set() and time.monotonic() < ends:
                active = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({active, closed_task}, timeout=max(0, ends-time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if active not in done or closed.is_set():
                    break
                event = active.result()
                active = asyncio.create_task(serve_turn(websocket, identity, engine, secret, event))
                done, _ = await asyncio.wait({active, closed_task}, timeout=max(0, ends-time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if closed_task in done or active not in done:
                    break
                try:
                    await active
                except GatewayError as error:
                    await asyncio.wait_for(websocket.send_json(error_event(error, event)), 30)
                except (WebSocketDisconnect, asyncio.TimeoutError):
                    raise
                except SQLAlchemyError:
                    await asyncio.wait_for(websocket.send_json(error_event(
                        GatewayError("storage_unavailable", 503, "storage"), event)), 30)
                except Exception:
                    await asyncio.wait_for(websocket.send_json(error_event(
                        GatewayError("internal_error", 500), event)), 30)
                active = None
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            for task in (active, read_task, closed_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (active, read_task, closed_task) if t is not None), return_exceptions=True)
            if not peer_disconnected:
                try:
                    await websocket.close(code=close_code)
                except (RuntimeError, WebSocketDisconnect):
                    pass
