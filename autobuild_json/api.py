"""Authenticated API; no login is performed until the operator starts a job."""
import json
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .api_models import AccountText, CallbackInput, ExportKind, JobInput, LinkInput, LoginInput
from .checklive_adapter import CheckliveProcessor
from .errors import FlowError
from .input_parser import parse_accounts
from .models import AttemptContext
from .oauth import OAuthSessions
from .proxies import parse_proxies
from .process_lock import ProcessLock
from .results import RunStore
from .runner import Runner
from .security import AdminSessions, BoundaryMiddleware, prepare_settings
from .tokens import TokenService
from .transport import SafeTransport


def create_app(settings, runner=None, oauth_sessions=None, token_service=None):
    admins, manual_oauth = None, None
    tokens = token_service or TokenService()
    store = runner.store if runner else RunStore(settings.data_dir / "runs")
    owners, owner_lock = {}, threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal admins, manual_oauth
        lock = ProcessLock(settings.data_dir)
        await run_in_threadpool(lock.acquire)
        try:
            await run_in_threadpool(prepare_settings, settings)
            admins = AdminSessions(settings.admin_token.get_secret_value())
            manual_oauth = oauth_sessions or OAuthSessions(settings)
            app.state.storage_ready = await run_in_threadpool(store.recover_interrupted, read_only_fallback=True)
            app.state.auth_ready = runner is not None
            app.state.runner = runner
            if runner is None:
                try:
                    processor = await run_in_threadpool(CheckliveProcessor, settings, OAuthSessions(settings), tokens)
                    app.state.auth_ready = True
                except FlowError:
                    processor = None
                app.state.runner = Runner(store, processor)
            try:
                yield
            finally:
                await run_in_threadpool(app.state.runner.close)
        finally:
            lock.release()

    app = FastAPI(title="AutoBuildJsonCockplit-Tools", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BoundaryMiddleware, port=settings.port)
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def authorized(request: Request):
        return admins.require(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic error input/locations can contain caller credentials. Return no
        # raw errors, locations, provider text, or dictionary key names.
        return JSONResponse({"error":"INVALID_REQUEST", "reason":"Invalid request fields"}, status_code=422)

    @app.exception_handler(FlowError)
    async def flow_error(request, exc):
        status = {"NOT_FOUND":404, "BATCH_CONFLICT":409, "CONFIGURATION_ERROR":503,
                  "STORAGE_ERROR":507, "RATE_LIMITED":429}.get(exc.code, 400)
        return JSONResponse({"error":exc.code, "reason":exc.reason, "details":exc.details}, status_code=status)

    @app.get("/")
    def root():
        return FileResponse(static_dir / "index.html", media_type="text/html")

    @app.post("/api/session")
    def login(payload: LoginInput, request: Request, response: Response):
        sid, csrf = admins.login(payload.token.get_secret_value(), request.cookies.get("autobuild_session"))
        response.set_cookie("autobuild_session", sid, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=28800)
        return {"csrf_token":csrf}

    @app.get("/api/session")
    def session(sid=Depends(authorized)):
        return {"csrf_token":admins.csrf(sid)}

    @app.delete("/api/session", status_code=204)
    def logout(sid=Depends(authorized)):
        admins.logout(sid)
        with owner_lock:
            for session_id, (owner, _) in list(owners.items()):
                if owner == sid:
                    manual_oauth.discard(session_id)
                    owners.pop(session_id, None)
        response = Response(status_code=204)
        response.delete_cookie("autobuild_session")
        return response

    @app.get("/api/health", dependencies=[Depends(authorized)])
    def health():
        return {"status":"ok" if app.state.storage_ready else "degraded", "auth_ready":app.state.auth_ready,
                "storage_ready":app.state.storage_ready}

    @app.post("/api/accounts/validate", dependencies=[Depends(authorized)])
    def validate(payload: AccountText):
        report = parse_accounts(payload.accounts_text.get_secret_value())
        preview = []
        for account in report.accounts:
            local, domain = account.email.split("@", 1)
            preview.append({"line_number":account.line_number, "email":local[:1]+"***@"+domain})
        return {"valid_count":len(report.accounts), "filtered_count":len(report.rejected),
                "filtered":[asdict(row) for row in report.rejected], "duplicate_lines":report.duplicate_lines, "preview":preview}

    @app.post("/api/jobs", status_code=202, dependencies=[Depends(authorized)])
    def start(payload: JobInput):
        if not app.state.storage_ready:
            raise FlowError("STORAGE_ERROR", "storage")
        report = parse_accounts(payload.accounts_text.get_secret_value())
        proxies = parse_proxies(payload.proxies_text.get_secret_value())
        if not app.state.auth_ready:
            raise FlowError("CONFIGURATION_ERROR", "dependency_check")
        return {"id":app.state.runner.start(report, proxies, payload.mode, payload.workers, payload.timeout)}

    @app.get("/api/jobs", dependencies=[Depends(authorized)])
    def history():
        return store.list_runs()

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authorized)])
    def status(job_id: str):
        return app.state.runner.get(job_id)

    @app.post("/api/jobs/{job_id}/stop", status_code=202, dependencies=[Depends(authorized)])
    def stop(job_id: str):
        if not app.state.storage_ready:
            raise FlowError("STORAGE_ERROR", "storage")
        app.state.runner.stop(job_id)
        return {"id":job_id, "stop_requested":True}

    @app.get("/api/jobs/{job_id}/exports/{kind}", dependencies=[Depends(authorized)])
    def export(job_id: str, kind: ExportKind):
        return Response(store.export(job_id, kind.value), media_type="application/json",
            headers={"Content-Disposition":f'attachment; filename="{kind.value}.json"'})

    @app.post("/api/oauth/links", status_code=201)
    def link(payload: LinkInput, sid=Depends(authorized)):
        proxies = parse_proxies(payload.proxy.get_secret_value())
        if len(proxies) > 1:
            raise FlowError("INVALID_INPUT", "proxy")
        with owner_lock:
            now = time.monotonic()
            for key, (_, expiry) in list(owners.items()):
                if expiry <= now:
                    owners.pop(key, None)
            item = manual_oauth.create(payload.email, proxies[0] if proxies else None)
            owners[item.session_id] = (sid, item.expires_at)
        return {"session_id":item.session_id, "desktop_url":item.desktop_url, "expires_in":600}

    @app.post("/api/oauth/complete")
    def complete(payload: CallbackInput, sid=Depends(authorized)):
        with owner_lock:
            owner = owners.get(payload.session_id)
            if owner is None or owner[0] != sid:
                raise FlowError("INVALID_STATE", "callback")
            grant = manual_oauth.claim(payload.session_id, payload.callback_url.get_secret_value())
            owners.pop(payload.session_id, None)
        context = AttemptContext(time.monotonic()+180, threading.Event(), lambda *args:None)
        transport = SafeTransport(grant.proxy, context)
        try:
            result = tokens.complete(grant, transport)
        finally:
            transport.close()
        return Response(json.dumps([result.model_dump()]), media_type="application/json",
            headers={"Content-Disposition":'attachment; filename="success.json"'})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error":exc.detail if isinstance(exc.detail, str) else "REQUEST_REJECTED"}, status_code=exc.status_code)

    return app
