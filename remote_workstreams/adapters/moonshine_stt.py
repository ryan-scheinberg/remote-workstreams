"""On-device Moonshine Voice streaming STT."""

from __future__ import annotations

import asyncio
import contextlib
import queue
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from remote_workstreams.adapters.stt import STTAdapter, TranscriptChunk
from remote_workstreams.audio.pcm import s16le_to_float32
from remote_workstreams.protocol import MIC_FORMAT

_STOP = object()
_MAX_AUDIO_CHUNKS = 100


class _WorkerError:
    def __init__(self, error: BaseException) -> None:
        self.error = error


def _default_transcriber(
    language: str, model: str, model_dir: Path, update_interval: float
) -> object:
    try:
        from moonshine_voice import ModelArch, Transcriber, get_model_for_language
    except ImportError as exc:  # pragma: no cover - exercised by deployment checks
        raise RuntimeError(
            "Moonshine is not installed; run `uv sync --extra local-voice`."
        ) from exc

    model_name = model.strip().lower().replace("_", "-")
    if model_name in {"tiny", "small", "base", "medium"}:
        model_name = f"{model_name}-streaming"
    arch_name = model_name.upper().replace("-", "_")
    try:
        model_arch = getattr(ModelArch, arch_name.upper())
    except AttributeError as exc:
        raise ValueError(
            f"unsupported Moonshine model {model!r}; use tiny, small, base, or medium-streaming"
        ) from exc
    model_path, resolved_arch = get_model_for_language(
        language, model_arch, cache_root=model_dir
    )
    return Transcriber(model_path=model_path, model_arch=resolved_arch, update_interval=update_interval)


class _Listener:
    def __init__(self, publish: Callable[[TranscriptChunk], None]) -> None:
        self._publish = publish
        self._last: tuple[str, bool] | None = None

    @staticmethod
    def _text(event: object) -> str:
        line = getattr(event, "line", event)
        return str(getattr(line, "text", "") or "").strip()

    def _emit(self, text: str, *, final: bool, endpoint: bool) -> None:
        if not text:
            return
        key = (text, final)
        if key == self._last:
            return
        self._last = key
        self._publish(
            TranscriptChunk(text=text, is_final=final, speech_final=endpoint, ts=time.time())
        )

    def on_line_started(self, event: object) -> None:
        self._emit(self._text(event), final=False, endpoint=False)

    def on_line_updated(self, event: object) -> None:
        self._emit(self._text(event), final=False, endpoint=False)

    def on_line_text_changed(self, event: object) -> None:
        self._emit(self._text(event), final=False, endpoint=False)

    def on_line_completed(self, event: object) -> None:
        self._emit(self._text(event), final=True, endpoint=True)

    def __call__(self, event: object) -> None:
        """Handle Moonshine's callable listener API across package versions."""
        line = getattr(event, "line", event)
        event_name = type(event).__name__.lower()
        final = event_name == "linecompleted" or bool(
            getattr(line, "is_complete", False)
        )
        self._emit(self._text(event), final=final, endpoint=final)


class MoonshineSTT(STTAdapter):
    """Moonshine STT behind the app's async streaming adapter contract."""

    def __init__(
        self,
        *,
        language: str = "en",
        model: str = "medium-streaming",
        model_dir: Path | str | None = None,
        update_interval: float = 0.25,
        transcriber_factory: Callable[[], object] | None = None,
    ) -> None:
        self.language = language
        self.model = model
        self.model_dir = Path(model_dir or Path.home() / ".remote-workstreams/models/moonshine")
        self.update_interval = update_interval
        self._factory = transcriber_factory

    def _make_transcriber(self) -> object:
        if self._factory is not None:
            return self._factory()
        self.model_dir.mkdir(parents=True, exist_ok=True)
        return _default_transcriber(self.language, self.model, self.model_dir, self.update_interval)

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptChunk]:
        loop = asyncio.get_running_loop()
        audio_queue: queue.Queue[list[float] | object] = queue.Queue(maxsize=_MAX_AUDIO_CHUNKS)
        events: asyncio.Queue[TranscriptChunk | object | _WorkerError] = asyncio.Queue()

        def publish(value: TranscriptChunk | object | _WorkerError) -> None:
            loop.call_soon_threadsafe(events.put_nowait, value)

        def worker() -> None:
            transcriber: object | None = None
            try:
                transcriber = self._make_transcriber()
                listener = _Listener(lambda chunk: publish(chunk))
                transcriber.add_listener(listener)  # type: ignore[attr-defined]
                transcriber.start()  # type: ignore[attr-defined]
                while True:
                    item = audio_queue.get()
                    if item is _STOP:
                        transcriber.stop()  # type: ignore[attr-defined]
                        publish(_STOP)
                        return
                    transcriber.add_audio(item, MIC_FORMAT.sample_rate)  # type: ignore[attr-defined]
            except BaseException as exc:
                publish(_WorkerError(exc))
            finally:
                close = getattr(transcriber, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        close()

        worker_task = asyncio.create_task(asyncio.to_thread(worker))

        async def pump() -> None:
            try:
                async for pcm in audio:
                    if not pcm:
                        continue
                    try:
                        audio_queue.put_nowait(s16le_to_float32(pcm))
                    except queue.Full as exc:
                        raise RuntimeError("Moonshine STT audio queue overflow") from exc
            except Exception as exc:
                publish(_WorkerError(exc))
                raise
            finally:
                while True:
                    try:
                        audio_queue.put_nowait(_STOP)
                        break
                    except queue.Full:
                        with contextlib.suppress(queue.Empty):
                            audio_queue.get_nowait()

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                value = await events.get()
                if isinstance(value, _WorkerError):
                    raise value.error
                if value is _STOP:
                    return
                yield value  # type: ignore[misc]
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            while True:
                try:
                    audio_queue.put_nowait(_STOP)
                    break
                except queue.Full:
                    with contextlib.suppress(queue.Empty):
                        audio_queue.get_nowait()
            with contextlib.suppress(Exception):
                await worker_task
