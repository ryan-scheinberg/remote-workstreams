"""The /ws endpoint. Speaks voicecode/protocol.py exactly: first text frame must
be Hello carrying a live session token (invalid → Error, close); on success Ready,
then chat history replay, then the loop. One live socket globally — a new
connection takes over.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from starlette.websockets import WebSocket

from voicecode import protocol
from voicecode.server.auth import LoginManager
from voicecode.server.logs import log
from voicecode.server.runtime import ConvoRuntime

logger = logging.getLogger("voicecode.server.ws")


class WSConnection:
    """runtime.ClientConnection over a Starlette WebSocket."""

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
    runtime: ConvoRuntime = websocket.app.state.runtime
    login: LoginManager = websocket.app.state.login
    await websocket.accept()
    conn = WSConnection(websocket)

    hello = await _receive_hello(websocket)
    if hello is None:
        await conn.close_with_error("expected hello")
        return
    if not login.session_ok(hello.credential):
        log(logger, "ws_auth_failed")
        await conn.close_with_error("invalid credential")
        return
    await conn.send_message(protocol.Ready())
    await runtime.attach(conn)  # replays chat history, then live entries stream

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
                await _handle(runtime, conn, text)
            except Exception:
                logger.exception("ws message handling failed")
                await conn.send_message(protocol.Error(message="internal error"))
    finally:
        await runtime.detach(conn)
        log(logger, "ws_closed")


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


async def _handle(runtime: ConvoRuntime, conn: WSConnection, text: str) -> None:
    try:
        msg = protocol.parse_client_message(text)
    except ValidationError:
        await conn.send_message(protocol.Error(message="invalid message"))
        return
    log(logger, "ws_message", msg_type=msg.type)

    if isinstance(msg, protocol.TextInput):
        if runtime.pipeline is not None:
            await runtime.pipeline.text(msg.text)
    elif isinstance(msg, protocol.Mute):
        if runtime.pipeline is not None:
            runtime.pipeline.set_muted(msg.muted)
    elif isinstance(msg, protocol.NewWorkstream):
        runtime.new_workstream()
    elif isinstance(msg, protocol.SendToWorkstream):
        runtime.send_to_workstream(msg.workstream)
    elif isinstance(msg, protocol.CheckIn):
        await runtime.check_in(msg.workstream)
    elif isinstance(msg, protocol.EndWorkstream):
        runtime.end_workstream(msg.workstream)
    elif isinstance(msg, protocol.Compact):
        await runtime.compact()
    elif isinstance(msg, protocol.ClearConvo):
        runtime.clear_convo()
    elif isinstance(msg, protocol.Approval):
        runtime.approvals.resolve(msg.approval_id, msg.approved)
    else:  # a second Hello mid-connection
        await conn.send_message(protocol.Error(message="already connected"))
