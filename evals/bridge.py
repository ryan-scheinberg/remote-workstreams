"""Bridge coherence evals: scripted conversations against the real ConversationEngine.

Default mode uses a mock model client — no network, assertions are structural
(events reach the prompt, dispatch is captured and never spoken, proactive turns
gate correctly, the system prompt contract holds). `--live` (env-gated behind
VOICECODE_LIVE_EVALS=1) runs the same scenarios against the real model, where the
spoken-text assertions become real behavioral checks. Ryan runs live; agents don't.

Run: uv run python -m evals.bridge
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from voicecode.engine import ConversationEngine
from voicecode.events import Completed, Finding, Progress, StatusEvent, TaskStarted

from evals.mockclient import MockClient, RecordingClient


@dataclass
class Scenario:
    name: str
    events: list[StatusEvent]
    user_text: str | None  # None -> proactive_turn()
    mock_reply: str  # what the mock model says; unused in --live
    expect_api_call: bool = True
    prompt_contains: list[str] = field(default_factory=list)
    prompt_excludes: list[str] = field(default_factory=list)
    spoken_contains: list[str] = field(default_factory=list)
    spoken_excludes: list[str] = field(default_factory=list)
    expect_dispatch: bool | None = None  # None = don't check
    dispatch_contains: list[str] = field(default_factory=list)


SCENARIOS = [
    Scenario(
        name="in-flight work is deferred, not claimed done",
        events=[TaskStarted(summary="Started refactoring the auth module.")],
        user_text="Hey, is it done yet?",
        mock_reply=(
            "Not yet — it's still working through the auth module. "
            "I'll speak up the second it finishes."
        ),
        prompt_contains=[
            "<system-reminder>",
            "[task_started]",
            "Started refactoring the auth module.",
        ],
        spoken_excludes=[
            "it's done",
            "it is done",
            "all done",
            "has finished",
            "finished successfully",
            "completed",
            "task is complete",
        ],
        expect_dispatch=False,
    ),
    Scenario(
        name="a finding surfaces in the next turn",
        events=[Finding(summary="The auth bug is in token refresh, not the login flow.")],
        user_text="Any updates on the bug hunt?",
        mock_reply=(
            "Yeah, one — turns out the auth bug lives in token refresh, not the login flow."
        ),
        prompt_contains=["[finding]", "token refresh"],
        spoken_contains=["token refresh"],
        expect_dispatch=False,
    ),
    Scenario(
        name="a completed event triggers proactive speech",
        events=[Completed(summary="Finished the auth module refactor and all tests pass.")],
        user_text=None,
        mock_reply=(
            "Good news — the auth module refactor just finished, and all the tests pass."
        ),
        prompt_contains=[
            "[completed]",
            "Finished the auth module refactor",
            "user hasn't said anything",
        ],
        spoken_contains=["auth"],
    ),
    Scenario(
        name="a coding request emits a dispatch, never spoken",
        events=[],
        user_text="Can you rename the config loader to settings and fix its tests?",
        mock_reply=(
            "Sure — I'll get that started now."
            "<dispatch>Rename the config loader to settings and update its tests</dispatch>"
        ),
        prompt_excludes=["<system-reminder>"],
        spoken_excludes=["<dispatch>", "</dispatch>", "dispatch"],
        expect_dispatch=True,
        dispatch_contains=["config loader"],
    ),
    Scenario(
        name="chit-chat: no dispatch, no fabricated status",
        events=[],
        user_text="Morning! How's it going?",
        mock_reply="Morning! All quiet over here — what are we building today?",
        prompt_excludes=["<system-reminder>"],
        spoken_excludes=["still working", "in progress", "task_started", "the task"],
        expect_dispatch=False,
    ),
    Scenario(
        name="progress-only queue stays quiet (no API call)",
        events=[Progress(summary="Now editing the token refresh logic.")],
        user_text=None,
        mock_reply="",
        expect_api_call=False,
    ),
]


def _check_system_contract(call: dict[str, Any], failures: list[str]) -> None:
    """Every request must carry the frozen, cache-marked system prompt."""
    system = call.get("system")
    if not isinstance(system, list) or not system:
        failures.append("system is not a block list")
        return
    final = system[-1]
    if final.get("cache_control") != {"type": "ephemeral"}:
        failures.append("final system block missing cache_control ephemeral")
    text = "".join(b.get("text", "") for b in system)
    for clause in ("NEVER fabricate", "<dispatch>", "<system-reminder>"):
        if clause not in text:
            failures.append(f"system prompt missing clause: {clause!r}")


async def run_scenario(scenario: Scenario, client: Any) -> list[str]:
    engine = ConversationEngine(client)
    engine.inject_events(scenario.events)

    chunks: list[str] = []
    if scenario.user_text is None:
        async for chunk in engine.proactive_turn():
            chunks.append(chunk)
    else:
        async for chunk in engine.turn(scenario.user_text):
            chunks.append(chunk)

    spoken = " ".join(chunks)
    spoken_cf = spoken.casefold()
    dispatch = engine.take_dispatch()
    failures: list[str] = []

    if not scenario.expect_api_call:
        if client.calls:
            failures.append("expected no API call, but one was made")
        if chunks:
            failures.append(f"expected silence, got speech: {spoken!r}")
        return failures

    if not client.calls:
        return ["expected an API call, none was made"]
    call = client.calls[-1]
    _check_system_contract(call, failures)

    prompt = call["messages"][-1]["content"]
    for needle in scenario.prompt_contains:
        if needle not in prompt:
            failures.append(f"prompt missing {needle!r}")
    for needle in scenario.prompt_excludes:
        if needle in prompt:
            failures.append(f"prompt unexpectedly contains {needle!r}")

    if not chunks:
        failures.append("expected speech, got none")
    for needle in scenario.spoken_contains:
        if needle.casefold() not in spoken_cf:
            failures.append(f"spoken text missing {needle!r}: {spoken!r}")
    for needle in scenario.spoken_excludes:
        if needle.casefold() in spoken_cf:
            failures.append(f"spoken text contains forbidden {needle!r}: {spoken!r}")

    if scenario.expect_dispatch is True:
        if dispatch is None:
            failures.append("expected a dispatch, got none")
        else:
            for needle in scenario.dispatch_contains:
                if needle.casefold() not in dispatch.casefold():
                    failures.append(f"dispatch missing {needle!r}: {dispatch!r}")
    elif scenario.expect_dispatch is False and dispatch is not None:
        failures.append(f"unexpected dispatch: {dispatch!r}")

    return failures


def _make_client(live: bool) -> Any:
    if not live:
        return None  # per-scenario MockClient built in main loop
    if os.environ.get("VOICECODE_LIVE_EVALS") != "1":
        sys.exit("--live requires VOICECODE_LIVE_EVALS=1 (Ryan runs live evals)")
    from anthropic import AsyncAnthropic

    from voicecode.keychain import get_secret

    api_key = get_secret("anthropic-api-key")
    if not api_key:
        sys.exit("--live requires an Anthropic API key (env or Keychain)")
    return AsyncAnthropic(api_key=api_key)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge coherence evals")
    parser.add_argument("--live", action="store_true", help="run against the real model")
    args = parser.parse_args()

    live_client = _make_client(args.live)
    failed = 0
    for scenario in SCENARIOS:
        client: Any
        if args.live:
            client = RecordingClient(live_client)
        else:
            client = MockClient([scenario.mock_reply])
        failures = await run_scenario(scenario, client)
        if failures:
            failed += 1
            print(f"FAIL  {scenario.name}")
            for failure in failures:
                print(f"      - {failure}")
        else:
            print(f"PASS  {scenario.name}")

    total = len(SCENARIOS)
    print(f"\n{total - failed}/{total} scenarios passed"
          f" ({'live' if args.live else 'mock'} model)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
