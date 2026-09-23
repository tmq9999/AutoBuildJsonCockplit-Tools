import contextvars
import importlib
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urljoin

from .challenges import continuation, inspect_response
from .errors import FlowError
from .models import RunResult
from .navigation import OAuthNavigator
from .oauth import ISSUER
from .transport import SafeTransport

AUTH_REPO = "https://github.com/tmq9999/Check-Account-ChatGPT.git"
AUTH_COMMIT = "791beb350370c191bbe8ddbe0b4a3fa0072620d9"
_load_lock = threading.Lock()
_attempt_context = contextvars.ContextVar("checklive_attempt_context", default=None)
_sandbox = None


def load_checklive(path: Path):
    global _sandbox
    path = path.resolve()
    try:
        if not (path / "checklive/auth.py").is_file():
            raise ValueError()
        revision = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True, timeout=5).stdout
        if revision != AUTH_COMMIT or dirty:
            raise ValueError()
        with _load_lock:
            loaded = sys.modules.get("checklive.auth")
            if loaded:
                if Path(loaded.__file__).resolve() != path / "checklive/auth.py":
                    raise ValueError()
                return loaded
            if "checklive" in sys.modules:
                raise ValueError()
            sys.path.insert(0, str(path))
            config = importlib.import_module("checklive.config")
            _sandbox = tempfile.TemporaryDirectory(prefix="autobuild-checklive-")
            config.WORKSPACE_ROOT = Path(_sandbox.name)
            config.DEBUG = False
            module = importlib.import_module("checklive.auth")
            original_config = module.sentinel._config

            def bounded_config(*args, **kwargs):
                context = _attempt_context.get()
                if context is not None:
                    context.check()
                return original_config(*args, **kwargs)

            module.sentinel._config = bounded_config
            required = ("visit_homepage", "get_csrf_token", "get_auth_url", "init_oauth", "authorize_continue", "verify_password", "mfa_issue_challenge", "mfa_verify")
            if not all(callable(getattr(module.AuthSession, name, None)) for name in required):
                raise ValueError()
            return module
    except Exception:
        raise FlowError("CONFIGURATION_ERROR", "dependency_check") from None


class AuthTransport(SafeTransport):
    fatal_error = None

    def request(self, method, url, **kwargs):
        if self.fatal_error:
            raise self.fatal_error
        try:
            response = super().request(method, url, **kwargs)
            inspect_response(response, url)
            return response
        except FlowError as exc:
            # Upstream Sentinel catches exceptions and creates a fallback. Preserve
            # the error so the adapter cannot submit that fallback after a block.
            self.fatal_error = FlowError(exc.code, exc.stage)
            raise


def guarded_auth(module, transport):
    class GuardedAuth(module.AuthSession):
        def __init__(self):
            self.proxy = transport.proxy
            self.proxy_url = self.proxy.url if self.proxy else None
            self.proxies = {"http":self.proxy_url, "https":self.proxy_url} if self.proxy else None
            self.session = transport
            self.device_id = module.config.fresh_device_id()
            self.access_token = self.session_token = self.account_id = None

        def _debug(self, *args, **kwargs):
            return None

        def _request(self, method, url, **kwargs):
            if kwargs.pop("allow_redirects", True):
                return transport.follow(method, url, **kwargs)
            return transport.request(method, url, **kwargs)

        def _get_sentinel_token(self, flow="password_verify"):
            try:
                value = super()._get_sentinel_token(flow)
            except FlowError as exc:
                raise FlowError(exc.code, "sentinel") from None
            if transport.fatal_error:
                raise transport.fatal_error
            transport.context.check()
            return value

    return GuardedAuth()


class CheckliveProcessor:
    def __init__(self, settings, oauth_sessions, token_service, *, module=None, raw_factory=None):
        self.module = module or load_checklive(settings.checklive_path)
        self.oauth_sessions, self.token_service = oauth_sessions, token_service
        self.raw_factory = raw_factory

    def __call__(self, account, proxy, context):
        marker = _attempt_context.set(context)
        try:
            return self._process(account, proxy, context)
        finally:
            _attempt_context.reset(marker)

    def _process(self, account, proxy, context):
        for attempt in (1, 2):
            transport, oauth = None, None
            stage = "initialize"
            try:
                context.check()
                context.on_stage(stage, attempt)
                transport = AuthTransport(proxy, context, self.raw_factory() if self.raw_factory else None)
                auth = guarded_auth(self.module, transport)
                oauth = self.oauth_sessions.create(account.email, proxy)
                auth.visit_homepage()
                csrf = auth.get_csrf_token()
                login_url = auth.get_auth_url(account.email, csrf)
                auth.init_oauth(login_url)
                stage = "email"
                context.on_stage(stage, attempt)
                auth.authorize_continue(account.email)
                stage = "password"
                context.on_stage(stage, attempt)
                password_response = auth.verify_password(account.password)
                challenge_id = auth._extract_mfa_challenge_id(password_response)
                if not challenge_id and auth._is_mfa_page(password_response):
                    challenge_id = auth._extract_mfa_challenge_id(auth._client_auth_session_dump())
                continue_url = continuation(password_response)
                if challenge_id:
                    stage = "totp"
                    context.on_stage(stage, attempt)
                    auth.mfa_issue_challenge(challenge_id)
                    code = self.module.generate_totp(account.secret)
                    mfa_response = auth.mfa_verify(challenge_id, code)
                    continue_url = continuation(mfa_response)
                if not continue_url:
                    raise FlowError("MFA_ERROR", stage)
                transport.follow("GET", urljoin(ISSUER, continue_url))
                stage = "codex_oauth"
                context.on_stage(stage, attempt)
                callback = OAuthNavigator(transport, context).complete(oauth.authorize_url)
                grant = self.oauth_sessions.claim(oauth.session_id, callback)
                stage = "token_exchange"
                context.on_stage(stage, attempt)
                # Token exchange has its own error semantics; preserve session/proxy
                # without the auth-response classifier changing exchange failures.
                exchange = SafeTransport(proxy, context, transport.raw)
                record = self.token_service.complete(grant, exchange)
                return RunResult("success", record=record, attempt=attempt)
            except Exception as exc:
                if isinstance(exc, FlowError):
                    error = FlowError(exc.code, "sentinel" if exc.stage == "sentinel" else stage)
                elif isinstance(exc, self.module.InvalidCredentialsError):
                    error = FlowError("MFA_ERROR" if stage == "totp" else "INVALID_CREDENTIALS", stage)
                elif isinstance(exc, self.module.AccountDeactivatedError):
                    error = FlowError("ACCOUNT_DEACTIVATED", stage)
                elif isinstance(exc, self.module.InvalidStateError):
                    error = FlowError("INVALID_STATE", stage)
                else:
                    error = FlowError("MFA_ERROR" if stage == "totp" else "AUTH_BLOCKED", stage)
                if error.code == "INVALID_STATE" and attempt == 1:
                    continue
                status = "phone_verify" if error.code == "PHONE_VERIFY" else "cancelled" if error.code == "CANCELLED" else "error"
                return RunResult(status, error=error, attempt=attempt)
            finally:
                if oauth:
                    self.oauth_sessions.discard(oauth.session_id)
                if transport:
                    transport.close()
