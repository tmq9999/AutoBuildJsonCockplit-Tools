import asyncio

from starlette.responses import Response

from ..contracts import InferenceEvent
from ..errors import GatewayError


class GatewayStreamResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, prepared, codec):
        super().__init__(content=None, status_code=200, media_type=getattr(codec, "media_type", self.media_type),
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
        self.prepared, self.codec = prepared, codec

    async def __call__(self, scope, receive, send):
        parent = asyncio.current_task()
        disconnected = False
        async def watch():
            nonlocal disconnected
            while True:
                if (await receive())["type"] == "http.disconnect":
                    disconnected = True
                    parent.cancel()
                    return
        watcher = asyncio.create_task(watch())
        try:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            try:
                async for event in self.prepared.events():
                    for chunk in self.codec.encode_event(event):
                        await asyncio.wait_for(send({"type": "http.response.body", "body": chunk, "more_body": True}), 60)
            except Exception as exc:
                error = exc if isinstance(exc, GatewayError) else GatewayError("storage_unavailable", 503, "stream")
                for chunk in self.codec.encode_event(InferenceEvent(kind="error", error_code=error.code)):
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            if not disconnected:
                raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await asyncio.wait_for(asyncio.shield(self.prepared.close()), 5)
