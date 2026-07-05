"""FastAPI app factory. Every collaborator is injected so tests run on fakes;
__main__.py (and voicecode/ambient.py) are the only modules that wire concretes.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles

from voicecode.config import Config
from voicecode.server import api, ws
from voicecode.server.approvals import Approvals
from voicecode.server.auth import LoginManager, PairingManager
from voicecode.server.logs import log, setup_logging
from voicecode.server.runtime import (
    ClientPush,
    ConvoBridge,
    ConvoRuntime,
    PipelineFactory,
    STTFactory,
    TTSFactory,
)
from voicecode.server.store import Store
from voicecode.server.workstreams import WorkstreamManager
from voicecode.substrate import Substrate

logger = logging.getLogger("voicecode.server.http")


class SPAStaticFiles(StaticFiles):
    """The static PWA at / with index.html fallback for client-side routes."""

    async def check_config(self) -> None:
        try:
            await super().check_config()
        except RuntimeError:
            pass  # web dir not built yet (the PWA unit fills it) — requests 404

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api/"):
                raise
            try:
                return await super().get_response("index.html", scope)
            except StarletteHTTPException:
                return PlainTextResponse("voice-code: PWA not built", status_code=404)


def create_app(
    config: Config,
    *,
    store: Store,
    bridge: ConvoBridge,
    substrate: Substrate,
    convo_transcript: Path,
    stt_factory: STTFactory,
    tts_factory: TTSFactory,
    pipeline_factory: PipelineFactory,
    approvals_token: str,
    plugin_dir: Path,
    workstream_settings: Path,
    web_dir: Path | None = None,
) -> FastAPI:
    setup_logging()
    push = ClientPush()
    workstreams = WorkstreamManager(
        substrate,
        store,
        push,
        convo_transcript=convo_transcript,
        data_dir=config.data_dir,
        plugin_dir=plugin_dir,
        settings_file=workstream_settings,
    )
    approvals = Approvals(push)
    runtime = ConvoRuntime(
        bridge,
        push,
        workstreams,
        approvals,
        stt_factory=stt_factory,
        tts_factory=tts_factory,
        pipeline_factory=pipeline_factory,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.start()
        yield
        await runtime.shutdown()
        store.close()

    app = FastAPI(title="voice-code", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.runtime = runtime
    app.state.approvals = approvals
    app.state.approvals_token = approvals_token
    app.state.pairing = PairingManager(store)
    app.state.login = LoginManager(store)

    @app.middleware("http")
    async def log_requests(request, call_next):
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request failed")
            raise
        log(
            logger,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
        # Revalidate everything: iOS otherwise keeps serving a stale app.js
        # across deploys (heuristic caching — the PWA has no build hashes).
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.include_router(router=api.router)
    app.add_api_websocket_route("/ws", ws.websocket_endpoint)
    web = web_dir or Path(__file__).resolve().parent.parent / "web"
    app.mount("/", SPAStaticFiles(directory=web, html=True, check_dir=False), name="web")
    return app
