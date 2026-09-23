import base64
import json
import re
from urllib.parse import urljoin, urlsplit

from .challenges import continuation, inspect_response
from .errors import FlowError
from .oauth import ISSUER, is_callback
from .transport import validate_url


class OAuthNavigator:
    def __init__(self, transport, context):
        self.transport, self.context = transport, context

    def _action(self, url, body, referer):
        response = self.transport.request("POST", url, json=body, headers={
            "accept":"application/json", "content-type":"application/json", "origin":ISSUER, "referer":referer})
        payload = inspect_response(response, url)
        target = continuation(payload) or response.headers.get("Location", "")
        if not target:
            raise FlowError("ACTION_REQUIRED", "consent")
        return urljoin(url, target)

    def _workspaces(self, html):
        text = html.replace('\\"', '"')
        ids = set(re.findall(r'"(?:workspace_id|workspaceId)"\s*:\s*"([0-9a-fA-F-]{36})"', text))
        cookies = getattr(self.transport, "cookies", {})
        value = cookies.get("oai-client-auth-session", "")
        for part in str(value).split(".")[:2]:
            try:
                data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                if isinstance(data, dict):
                    if isinstance(data.get("workspace_id"), str):
                        ids.add(data["workspace_id"])
                    for ws in data.get("workspaces", []):
                        if isinstance(ws, dict) and isinstance(ws.get("id"), str):
                            ids.add(ws["id"])
            except (ValueError, TypeError, UnicodeError):
                continue
        return ids

    def complete(self, authorize_url):
        current = authorize_url
        for _ in range(16):
            self.context.check()
            if is_callback(current):
                return current
            validate_url(current)
            if urlsplit(current).hostname != "auth.openai.com":
                raise FlowError("AUTH_BLOCKED", "oauth_redirect")
            response = self.transport.request("GET", current)
            payload = inspect_response(response, current)
            if response.status_code in {301,302,303,307,308}:
                target = response.headers.get("Location", "")
                if not target:
                    raise FlowError("AUTH_BLOCKED", "oauth_redirect")
                current = urljoin(current, target)
            elif "/choose-an-account" in urlsplit(current).path:
                ids = set(re.findall(r"us_[A-Za-z0-9]{16,}", response.text))
                if len(ids) != 1:
                    raise FlowError("WORKSPACE_SELECTION_REQUIRED", "account_selection")
                current = self._action(ISSUER + "/api/accounts/session/select", {"session_id":next(iter(ids))}, current)
            elif any(word in urlsplit(current).path for word in ("/consent", "/workspace", "/sign-in-with-chatgpt/")):
                ids = self._workspaces(response.text)
                if len(ids) != 1:
                    raise FlowError("WORKSPACE_SELECTION_REQUIRED", "workspace_selection")
                current = self._action(ISSUER + "/api/accounts/workspace/select", {"workspace_id":next(iter(ids))}, current)
            elif continuation(payload):
                current = urljoin(current, continuation(payload))
            else:
                raise FlowError("ACTION_REQUIRED", "oauth_navigation")
        raise FlowError("AUTH_BLOCKED", "redirect_limit")
