import json


class SSEDecoder:
    def __init__(self, max_frame_bytes=1_048_576):
        self.limit = max_frame_bytes
        self.buffer = bytearray()
        self.lines = []
        self.frame_size = 0
        self.first_line = True

    def feed(self, chunk):
        self.buffer.extend(chunk)
        result = []
        while b"\n" in self.buffer:
            position = self.buffer.index(b"\n")
            line = bytes(self.buffer[:position]).removesuffix(b"\r")
            del self.buffer[:position+1]
            self.frame_size += position+1
            if self.frame_size > self.limit:
                raise ValueError("stream_frame_too_large")
            if self.first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                self.first_line = False
            if line:
                self.lines.append(line.decode("utf-8"))
                continue
            event, data = "message", []
            for text in self.lines:
                field, _, value = text.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
            if data:
                result.append((event, "\n".join(data)))
            self.lines.clear()
            self.frame_size = 0
        if self.frame_size+len(self.buffer) > self.limit:
            raise ValueError("stream_frame_too_large")
        return result

    def finish(self):
        if self.buffer or self.lines:
            raise ValueError("truncated_stream")
        return []


class NDJSONDecoder:
    def __init__(self, max_frame_bytes=1_048_576):
        self.limit, self.buffer = max_frame_bytes, bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        result = []
        while b"\n" in self.buffer:
            position = self.buffer.index(b"\n")
            if position > self.limit:
                raise ValueError("stream_frame_too_large")
            line = bytes(self.buffer[:position])
            del self.buffer[:position+1]
            if line.strip():
                result.append(json.loads(line.decode("utf-8")))
        if len(self.buffer) > self.limit:
            raise ValueError("stream_frame_too_large")
        return result

    def finish(self):
        if self.buffer.strip():
            raise ValueError("truncated_stream")
        return []
