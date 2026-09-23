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
