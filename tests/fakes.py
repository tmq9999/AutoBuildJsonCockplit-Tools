import json

class Response:
    def __init__(self, payload=None, status=200, location=None, text=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.content = (text if text is not None else json.dumps(payload or {})).encode()

    @property
    def text(self):
        return self.content.decode()

    def json(self):
        return json.loads(self.text)

class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True
