import base64
import json
import re
from urllib.parse import unquote, urljoin, urlsplit
from uuid import UUID

from .challenges import continuation, inspect_response
from .errors import FlowError
from .oauth import ISSUER, is_callback
from .transport import validate_url
from .diagnostics import request_details


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
        value = unquote(str(cookies.get("oai-client-auth-session", "")))
        selected, available = set(), set()
        candidates = [value]
        for part in value.split(".")[:2]:
            try:
                candidates.append(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())
            except (ValueError, TypeError, UnicodeError):
                continue
        def uuid_string(value):
            try:
                return str(UUID(value)) if isinstance(value,str) else None
            except ValueError:
                return None
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            if not isinstance(data,dict):
                continue
            current = uuid_string(data.get("workspace_id"))
            if current:
                selected.add(current)
            workspaces = data.get("workspaces", [])
            if isinstance(workspaces,list):
                for ws in workspaces:
                    value = uuid_string(ws.get("id")) if isinstance(ws,dict) else None
                    if value:
                        available.add(value)
        if len(selected) == 1 and (not available or selected <= available):
            return selected
        if selected:
            return ids | selected | available
        return ids | available

    @staticmethod
    def _require_unique(ids, kind, url, status):
        details = {**request_details("GET",url), "http_status":status,
            "selection_kind":kind, "candidate_count":len(ids)}
        if not ids:
            raise FlowError("OAUTH_PARSE_ERROR", kind+"_selection", details=details)
        if len(ids) != 1:
            code = "WORKSPACE_SELECTION_REQUIRED" if kind == "workspace" else "ACCOUNT_SELECTION_REQUIRED"
            raise FlowError(code,kind+"_selection",details=details)
        return next(iter(ids))

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
                session_id = self._require_unique(ids,"account",current,response.status_code)
                current = self._action(ISSUER + "/api/accounts/session/select", {"session_id":session_id}, current)
            elif any(word in urlsplit(current).path for word in ("/consent", "/workspace", "/sign-in-with-chatgpt/")):
                ids = self._workspaces(response.text)
                workspace_id = self._require_unique(ids,"workspace",current,response.status_code)
                current = self._action(ISSUER + "/api/accounts/workspace/select", {"workspace_id":workspace_id}, current)
            elif continuation(payload):
                current = urljoin(current, continuation(payload))
            else:
                raise FlowError("ACTION_REQUIRED", "oauth_navigation")
        raise FlowError("AUTH_BLOCKED", "redirect_limit")
