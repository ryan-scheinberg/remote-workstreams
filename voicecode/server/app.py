"""FastAPI app factory. Every collaborator is injected so tests run on fakes;
__main__.py is the only module that wires the concrete implementations.
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
from voicecode.server.auth import PairingManager
from voicecode.server.logs import log, setup_logging
from voicecode.server.sessions import (
    EngineFactory,
    ExecutionFactory,
    PipelineFactory,
    SessionManager,
    STTFactory,
    TTSFactory,
)
from voicecode.server.store import Store

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
    engine_factory: EngineFactory,
    execution_factory: ExecutionFactory,
    stt_factory: STTFactory,
    tts_factory: TTSFactory,
    pipeline_factory: PipelineFactory,
    web_dir: Path | None = None,
) -> FastAPI:
    setup_logging()
    store = Store(config.db_path)
    manager = SessionManager(
        store,
        engine_factory=engine_factory,
        execution_factory=execution_factory,
        stt_factory=stt_factory,
        tts_factory=tts_factory,
        pipeline_factory=pipeline_factory,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.shutdown()
        store.close()

    app = FastAPI(title="voice-code", lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.manager = manager
    app.state.pairing = PairingManager(store)

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
        return response

    app.include_router(router=api.router)
    app.add_api_websocket_route("/ws", ws.websocket_endpoint)
    web = web_dir or Path(__file__).resolve().parent.parent / "web"
    app.mount("/", SPAStaticFiles(directory=web, html=True, check_dir=False), name="web")
    return app
