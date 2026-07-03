"""The /ws endpoint. Speaks voicecode/protocol.py exactly: first text frame must
be Hello (invalid credential → Error, close); on success Ready, then the loop.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from starlette.websockets import WebSocket

from voicecode import protocol
from voicecode.server.auth import credential_ok
from voicecode.server.logs import log
from voicecode.server.sessions import SessionManager, SessionRuntime, UnknownSession
from voicecode.server.store import Store

logger = logging.getLogger("voicecode.server.ws")


class WSConnection:
    """sessions.ClientConnection over a Starlette WebSocket."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.closed = False

    async def send_message(self, message: object) -> None:
        if self.closed:
            return
        try:
            await self.websocket.send_text(message.model_dump_json())  # type: ignore[attr-defined]
        except Exception:
            self.closed = True

    async def send_audio(self, pcm: bytes) -> None:
        if self.closed:
            return
        try:
            await self.websocket.send_bytes(pcm)
        except Exception:
            self.closed = True

    async def close_with_error(self, message: str) -> None:
        await self.send_message(protocol.Error(message=message))
        if not self.closed:
            self.closed = True
            try:
                await self.websocket.close(code=1008)
            except Exception:
                pass


async def websocket_endpoint(websocket: WebSocket) -> None:
    manager: SessionManager = websocket.app.state.manager
    store: Store = websocket.app.state.store
    await websocket.accept()
    conn = WSConnection(websocket)

    hello = await _receive_hello(websocket)
    if hello is None:
        await conn.close_with_error("expected hello")
        return
    if not credential_ok(store, hello.credential):
        log(logger, "ws_auth_failed")
        await conn.close_with_error("invalid credential")
        return
    try:
        runtime = await manager.attach(hello.session_id, conn)
    except UnknownSession:
        await conn.close_with_error("unknown session")
        return
    await conn.send_message(protocol.Ready(session_id=runtime.session_id))

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if runtime.conn is not conn:  # another connection took over
                break
            if (pcm := message.get("bytes")) is not None:
                if runtime.pipeline is not None:
                    await runtime.pipeline.feed(pcm)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                runtime = await _handle(manager, store, conn, runtime, text)
            except Exception:
                logger.exception("ws message handling failed")
                await conn.send_message(protocol.Error(message="internal error"))
    finally:
        if runtime.conn is conn:
            await manager.detach(runtime)
        log(logger, "ws_closed", session_id=runtime.session_id)


async def _receive_hello(websocket: WebSocket) -> protocol.Hello | None:
    message = await websocket.receive()
    text = message.get("text")
    if message["type"] != "websocket.receive" or text is None:
        return None
    try:
        parsed = protocol.parse_client_message(text)
    except ValidationError:
        return None
    return parsed if isinstance(parsed, protocol.Hello) else None


async def _handle(
    manager: SessionManager,
    store: Store,
    conn: WSConnection,
    runtime: SessionRuntime,
    text: str,
) -> SessionRuntime:
    """Handle one client text frame; returns the (possibly switched) runtime."""
    try:
        msg = protocol.parse_client_message(text)
    except ValidationError:
        await conn.send_message(protocol.Error(message="invalid message"))
        return runtime
    log(logger, "ws_message", session_id=runtime.session_id, msg_type=msg.type)

    if isinstance(msg, protocol.TextInput):
        if runtime.pipeline is not None:
            await runtime.pipeline.text(msg.text)
    elif isinstance(msg, protocol.Mute):
        if runtime.pipeline is not None:
            runtime.pipeline.set_muted(msg.muted)
    elif isinstance(msg, protocol.Approval):
        await runtime.execution.approve(msg.gate_id, msg.approved)
    elif isinstance(msg, protocol.SwitchSession):
        if store.get_session(msg.session_id) is None:
            await conn.send_message(protocol.Error(message="unknown session"))
            return runtime
        await manager.detach(runtime)
        runtime = await manager.attach(msg.session_id, conn)
        await conn.send_message(protocol.Ready(session_id=runtime.session_id))
    else:  # a second Hello mid-connection
        await conn.send_message(protocol.Error(message="already connected"))
    return runtime
