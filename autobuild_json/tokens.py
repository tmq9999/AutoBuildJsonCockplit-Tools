import threading
import time
import uuid

import jwt

from .errors import FlowError
from .models import AccountIdentity, SuccessRecord, TokenSet
from .oauth import CLIENT_ID, ISSUER
from .transport import object_json


class TokenService:
    def __init__(self):
        self._keys = {}
        self._lock = threading.Lock()

    def _key(self, kid, transport):
        proxy_key = transport.proxy.key if getattr(transport, "proxy", None) else "direct"
        with self._lock:
            cached_at, keys = self._keys.get(proxy_key, (0, []))
            if cached_at + 300 > time.monotonic():
                matches = [key for key in keys if key.get("kid") == kid]
                if len(matches) == 1:
                    return jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
            response = transport.request("GET", ISSUER + "/.well-known/jwks.json")
            if response.status_code != 200:
                raise FlowError("INVALID_TOKEN_RESPONSE", "jwks")
            payload = object_json(response, "INVALID_TOKEN_RESPONSE")
            keys = payload.get("keys", [])
            if not isinstance(keys, list) or len(keys) > 100:
                raise FlowError("INVALID_TOKEN_RESPONSE", "jwks")
            keys = [k for k in keys if isinstance(k, dict) and k.get("kty") == "RSA"
                    and k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"]
            self._keys[proxy_key] = (time.monotonic(), keys)
            matches = [key for key in keys if key.get("kid") == kid]
            if len(matches) != 1:
                raise FlowError("INVALID_TOKEN_RESPONSE", "jwks")
            return jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key

    def complete(self, grant, transport):
        try:
            response = transport.request("POST", ISSUER + "/oauth/token", data={
                "grant_type":"authorization_code", "client_id":CLIENT_ID, "code":grant.code,
                "code_verifier":grant.verifier, "redirect_uri":grant.redirect_uri,
            })
        except FlowError as exc:
            if exc.code in {"NETWORK_ERROR", "PROXY_ERROR", "TIMEOUT"}:
                raise FlowError("TOKEN_EXCHANGE_UNCERTAIN", "token_exchange") from None
            raise
        if response.status_code == 429:
            raise FlowError("RATE_LIMITED", "token_exchange")
        if response.status_code != 200:
            raise FlowError("TOKEN_EXCHANGE_ERROR", "token_exchange")
        payload = object_json(response, "INVALID_TOKEN_RESPONSE")
        try:
            tokens = TokenSet(**{name:payload[name] for name in TokenSet.model_fields})
            header = jwt.get_unverified_header(tokens.id_token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError()
            claims = jwt.decode(tokens.id_token, self._key(header["kid"], transport), algorithms=["RS256"],
                audience=CLIENT_ID, issuer=ISSUER, options={"require":["exp", "iat", "iss", "aud", "sub"]})
            email = claims.get("email")
            account_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
            if not isinstance(email, str) or claims.get("email_verified") is not True or not isinstance(account_id, str) or not account_id:
                raise ValueError()
            if email.casefold() != grant.expected_email.casefold():
                raise FlowError("ACCOUNT_MISMATCH", "token_validation")
            return SuccessRecord(id=str(uuid.uuid4()), email=email, account=AccountIdentity(id=account_id), tokens=tokens)
        except FlowError:
            raise
        except Exception:
            raise FlowError("INVALID_TOKEN_RESPONSE", "token_validation") from None
