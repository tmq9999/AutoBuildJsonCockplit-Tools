from ..errors import GatewayError


def client_secret(request):
    values = []
    for name in ("authorization", "x-api-key", "x-goog-api-key"):
        headers = request.headers.getlist(name)
        if len(headers) > 1:
            raise GatewayError("invalid_api_key", 401, "auth")
        if headers:
            value = headers[0]
            if name == "authorization":
                prefix, _, value = value.partition(" ")
                if prefix.lower() != "bearer":
                    raise GatewayError("invalid_api_key", 401, "auth")
            values.append(value)
    if not values or not values[0] or any(value != values[0] for value in values):
        raise GatewayError("invalid_api_key", 401, "auth")
    return values[0]


def gateway_session(request):
    values = request.headers.getlist("x-gateway-session")
    if len(values) > 1:
        raise GatewayError("invalid_request")
    if not values:
        return None
    value = values[0]
    if not 1 <= len(value) <= 200 or any(not 32 <= ord(char) <= 126 for char in value):
        raise GatewayError("invalid_request")
    return value
