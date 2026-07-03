"""Ambient composition helpers — no audio devices, no live APIs."""

from __future__ import annotations

from voicecode import ambient
from voicecode.events import Completed


class FakeAdapter:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.sent: list[str] = []

    async def start(self, prompt: str) -> str:
        self.started.append(prompt)
        return "sess-1"

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def events(self):
        yield Completed(summary="The task finished.")


async def test_dispatcher_starts_then_sends() -> None:
    adapter = FakeAdapter()
    dispatcher = ambient.Dispatcher(adapter)
    started_hooks: list[bool] = []
    dispatcher.on_started = lambda: started_hooks.append(True)

    await dispatcher("first directive")
    await dispatcher("second directive")
    await dispatcher("third directive")

    assert adapter.started == ["first directive"]
    assert adapter.sent == ["second directive", "third directive"]
    assert dispatcher.session_id == "sess-1"
    assert started_hooks == [True]  # fired exactly once


async def test_pump_events_feeds_pipeline() -> None:
    class FakePipeline:
        def __init__(self) -> None:
            self.batches: list[list] = []

        async def on_events(self, events) -> None:
            self.batches.append(list(events))

    pipeline = FakePipeline()
    await ambient.pump_events(FakeAdapter(), pipeline)
    assert len(pipeline.batches) == 1
    assert pipeline.batches[0][0].type == "completed"


async def test_main_refuses_without_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ambient.keychain, "get_secret", lambda name: None)
    assert await ambient.main() == 2
    assert "Missing secrets" in capsys.readouterr().out
