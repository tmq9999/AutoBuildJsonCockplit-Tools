"""Images facade over Codex hosted image-generation Responses."""
import base64
import json
import time

from starlette.datastructures import UploadFile

from ..contracts import GeneratedImage, ImageGenerationTool
from ..errors import GatewayError
from .openai_responses import ResponsesCodec, response_usage, image_source


async def read_image_request(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip() == "application/json":
        try:
            return await request.json()
        except ValueError:
            raise GatewayError("invalid_request") from None
    # Boundary has already bounded the whole body to 16 MiB. Starlette's
    # multipart parser preserves file bytes and case-sensitive boundaries.
    try:
        async with request.form(max_files=17, max_fields=32, max_part_size=16*1024*1024) as form:
            body, images = {}, []
            for key, value in form.multi_items():
                if key in {"image", "image[]"}:
                    if not isinstance(value, UploadFile):
                        raise ValueError()
                    images.append(await file_source(value))
                elif key == "mask":
                    if key in body or not isinstance(value, UploadFile):
                        raise ValueError()
                    body[key] = await file_source(value)
                else:
                    if key in body or not isinstance(value, str):
                        raise ValueError()
                    if key in {"n", "output_compression", "partial_images"}:
                        value = int(value)
                    elif key == "stream":
                        if value not in {"true", "false"}:
                            raise ValueError()
                        value = value == "true"
                    body[key] = value
            body["image"] = images
            return body
    except (ValueError, TypeError, UnicodeError):
        raise GatewayError("invalid_request") from None


async def file_source(upload):
    if upload.content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError()
    value = await upload.read(12*1024*1024+1)
    if not value or len(value) > 12*1024*1024:
        raise ValueError()
    return f"data:{upload.content_type};base64,{base64.b64encode(value).decode()}"


class ImagesCodec:
    def __init__(self, *, edit=False):
        self.edit = edit
        self.format = "b64_json"
        self.created = int(time.time())

    def decode(self, body):
        tool_fields = set(ImageGenerationTool.model_fields)-{"type", "model", "action", "input_image_mask"}
        if not isinstance(body, dict):
            raise GatewayError("invalid_request")
        if set(body)-tool_fields-{"model", "prompt", "n", "response_format", "stream", "image", "mask"}:
            raise GatewayError("unsupported_feature")
        if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
            raise GatewayError("unsupported_feature")
        if body.get("response_format", "b64_json") not in {"b64_json", "url"}:
            raise GatewayError("invalid_request")
        if not isinstance(body.get("prompt"), str) or not body["prompt"].strip() or len(body["prompt"]) > 32768:
            raise GatewayError("invalid_request")
        if type(body.get("stream", False)) is not bool:
            raise GatewayError("invalid_request")
        sources = body.get("image", [])
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list) or len(sources) > 16 or (self.edit and not sources):
            raise GatewayError("invalid_request")
        if not self.edit and (sources or "mask" in body):
            raise GatewayError("invalid_request")
        self.format = body.get("response_format", "b64_json")
        tool = {"type": "image_generation", "action": "edit" if self.edit else "generate",
                **{key: body[key] for key in tool_fields if key in body}}
        if "mask" in body:
            tool["input_image_mask"] = {"image_url": image_source(body["mask"])}
        payload = {"model": body.get("model", "gpt-image-2.5"),
            "input": [{"role": "user", "content": [{"type": "input_text", "text": body["prompt"]}]+
                [{"type": "input_image", "image_url": image_source(source)} for source in sources]}],
            "tools": [tool], "tool_choice": "required", "stream": body.get("stream", False)}
        return ResponsesCodec().decode(payload, {})

    def image(self, block):
        if not isinstance(block, GeneratedImage) or not block.result or block.status != "completed":
            raise GatewayError("upstream_error", 502, "upstream")
        result = ({"b64_json": block.result} if self.format == "b64_json" else
                  {"url": f"data:image/{block.output_format or 'png'};base64,{block.result}"})
        if block.revised_prompt:
            result["revised_prompt"] = block.revised_prompt
        return result

    def encode_result(self, result):
        images = [self.image(block) for block in result.blocks if isinstance(block, GeneratedImage)]
        if not images:
            raise GatewayError("upstream_error", 502, "upstream")
        return {"created": self.created, "data": images, "usage": response_usage(result.usage)}

    def encode_event(self, event):
        prefix = "image_edit" if self.edit else "image_generation"
        if event.kind == "image_partial":
            value = {"type": prefix+".partial_image", "b64_json": event.block.result,
                     "partial_image_index": event.summary_index, "created_at": self.created}
        elif event.kind == "block_finished" and isinstance(event.block, GeneratedImage):
            # Do not release terminal images until usage has been settled.
            self.completed = getattr(self, "completed", [])+[event.block]
            return []
        elif event.kind == "finished":
            if not getattr(self, "completed", []):
                raise GatewayError("upstream_error", 502, "stream")
            return [self._frame({"type": prefix+".completed", "created_at": self.created,
                **self.image(block), "usage": response_usage(event.usage)}) for block in self.completed]
        elif event.kind == "error":
            value = {"type": "error", "code": event.error_code, "message": event.error_code}
        else:
            return []
        return [self._frame(value)]

    @staticmethod
    def _frame(value):
        return ("event: "+value["type"]+"\ndata: "+json.dumps(value, separators=(",", ":"))+"\n\n").encode()
