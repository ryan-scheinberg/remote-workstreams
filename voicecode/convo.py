"""ConvoBridge: the voice/UI face of the `voice:convo` Claude Code session.

Writes go through the tmux substrate (send/slash); reads come from tailing the
session's transcript JSONL. run() polls the tail and fans every entry out to
subscribers; turn() additionally streams TTS-ready sentence chunks until the
transcript's turn_duration marker (TurnEnd) arrives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voicecode.audio.chunker import SentenceChunker
from voicecode.substrate import CCSession, Substrate
from voicecode.transcript import AssistantText, Entry, TranscriptTail, TurnEnd


class ConvoBridge:
    def __init__(
        self, substrate: Substrate, session: CCSession, poll_interval: float = 0.25
    ) -> None:
        self._substrate = substrate
        self._session = session
        self._poll = poll_interval
        self._tail = TranscriptTail(session.transcript)
        self._subscribers: set[asyncio.Queue[Entry | None]] = set()
        self._turn_queue: asyncio.Queue[Entry | None] | None = None
        self._unfinished = 0  # turns sent whose TurnEnd hasn't been seen yet
        self._closed = False

    async def run(self) -> None:
        """Poll the transcript, fanning new entries out, until close()."""
        while not self._closed:
            for entry in self._tail.read_new():
                for queue in self._subscribers:
                    queue.put_nowait(entry)
                if self._turn_queue is not None:
                    self._turn_queue.put_nowait(entry)
                if isinstance(entry, TurnEnd):
                    self._unfinished = max(0, self._unfinished - 1)
            await asyncio.sleep(self._poll)

    def subscribe(self) -> AsyncIterator[Entry]:
        """Live entries from now on (no history); every subscriber gets every entry."""
        queue: asyncio.Queue[Entry | None] = asyncio.Queue()
        self._subscribers.add(queue)

        async def entries() -> AsyncIterator[Entry]:
            try:
                while True:
                    entry = await queue.get()
                    if entry is None:
                        return
                    yield entry
            finally:
                self._subscribers.discard(queue)

        return entries()

    def history(self, limit: int = 200) -> list[Entry]:
        """The last `limit` entries, parsed fresh from the whole transcript file."""
        return TranscriptTail(self._session.transcript).read_new()[-limit:]

    def reset(self, session: CCSession) -> None:
        """Point at a brand-new convo session (Clear): fresh tail, any in-flight
        turn stream ends quietly. Subscribers stay attached."""
        self._session = session
        self._tail = TranscriptTail(session.transcript)
        self._unfinished = 0
        if self._turn_queue is not None:
            self._turn_queue.put_nowait(None)
            self._turn_queue = None

    async def send(self, text: str) -> None:
        await self._substrate.send(self._session, text)

    def turn(self, text: str) -> AsyncIterator[str]:
        """Send text, then stream TTS-ready sentence chunks until TurnEnd.

        Only one turn is active at a time: starting a new one detaches the old
        stream, which ends quietly. Input sent mid-turn queues in the session, so
        a superseded turn's remaining blocks and TurnEnd still land first — the
        new stream skips them (chat still gets them via subscribers) and speaks
        only its own reply. Tool/user entries are skipped here but still reach
        subscribers. Text blocks arrive complete, so each is chunked and flushed
        on its own — nothing is ever pending across blocks.
        """
        queue: asyncio.Queue[Entry | None] = asyncio.Queue()
        if self._turn_queue is not None:
            self._turn_queue.put_nowait(None)  # detach the superseded stream
        self._turn_queue = queue
        skip = self._unfinished  # superseded turns still owed a TurnEnd

        async def sentences() -> AsyncIterator[str]:
            await self.send(text)
            self._unfinished += 1
            nonlocal skip
            try:
                while True:
                    entry = await queue.get()
                    if entry is None:
                        return
                    if isinstance(entry, TurnEnd):
                        if skip == 0:
                            return
                        skip -= 1
                    elif isinstance(entry, AssistantText) and skip == 0:
                        chunker = SentenceChunker()
                        for sentence in chunker.feed(entry.text):
                            yield sentence
                        if tail := chunker.flush():
                            yield tail
            finally:
                if self._turn_queue is queue:
                    self._turn_queue = None

        return sentences()

    async def slash(self, command: str) -> None:
        await self._substrate.slash(self._session, command)

    async def close(self) -> None:
        """Stop run() and end all streams; the CC session keeps living in tmux."""
        self._closed = True
        for queue in self._subscribers:
            queue.put_nowait(None)
        if self._turn_queue is not None:
            self._turn_queue.put_nowait(None)
