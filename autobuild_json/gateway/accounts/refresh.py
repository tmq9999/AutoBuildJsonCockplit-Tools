import asyncio
from datetime import datetime, timezone
import threading
import time

import jwt

from ...models import AttemptContext
from ...oauth import CLIENT_ID, ISSUER
from ...tokens import TokenService
from ...transport import SafeTransport
from ..errors import GatewayError


def refresh_state(result):
    return {"success": "active", "invalid_grant": "reauth_required"}.get(result, "refresh_uncertain")


async def run_http_thread(call, context):
    task=asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        context.cancel.set()
        # Cancellation cannot release the proxy while a blocking curl call is live.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                context.cancel.set()
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def exchange_tokens(tokens, lease, deadline):
    context = AttemptContext(time.monotonic()+max(0, (deadline-datetime.now(timezone.utc)).total_seconds()),
                             threading.Event(), lambda *args: None)
    def exchange():
        transport = SafeTransport(lease.proxy, context)
        try:
            response = transport.post(ISSUER+"/oauth/token", data={"grant_type": "refresh_token",
                "client_id": CLIENT_ID, "refresh_token": tokens["refresh_token"]})
            try:
                payload = response.json()
            except ValueError:
                raise GatewayError("refresh_uncertain", 503, "refresh") from None
            if response.status_code != 200:
                if isinstance(payload, dict) and payload.get("error") == "invalid_grant":
                    raise GatewayError("reauth_required", 401, "refresh")
                raise GatewayError("refresh_uncertain", 503, "refresh")
            return payload
        finally:
            transport.close()
    try:
        return await run_http_thread(exchange,context)
    except asyncio.CancelledError:
        context.cancel.set()
        raise


async def verify_tokens(tokens, account, email, lease, deadline):
    context = AttemptContext(time.monotonic()+max(0, (deadline-datetime.now(timezone.utc)).total_seconds()),
                             threading.Event(), lambda *args: None)
    def verify():
        transport = SafeTransport(lease.proxy, context)
        try:
            header = jwt.get_unverified_header(tokens["id_token"])
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError()
            key = TokenService()._key(header["kid"], transport)
            claims = jwt.decode(tokens["id_token"], key, algorithms=["RS256"], audience=CLIENT_ID, issuer=ISSUER,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            if (claims.get("email", "").casefold() != email.casefold() or claims.get("email_verified") is not True
                    or claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") != account):
                raise ValueError()
        finally:
            transport.close()
    try:
        await run_http_thread(verify,context)
    except asyncio.CancelledError:
        context.cancel.set()
        raise
    except Exception:
        raise GatewayError("reauth_required", 401, "refresh") from None
