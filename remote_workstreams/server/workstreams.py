"""Workstreams: the execution sessions and the ephemeral planner/injector
passthroughs that feed them. All are real agent sessions (Claude Code or Codex)
in tmux; plans and directives travel through files the ephemeral sessions
write, polled here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from remote_workstreams import engines, protocol
from remote_workstreams.bootstrap import CONVO_MODEL
from remote_workstreams.server.logs import log
from remote_workstreams.server.store import Store
from remote_workstreams.substrate import CCSession, SessionSpec, Substrate
from remote_workstreams.transcript import AssistantText

logger = logging.getLogger("remote_workstreams.server.workstreams")

# Session roster: ephemeral passthroughs think hard, workstreams execute.
# Models are only defaults — store settings win (the menu sets workstream_model;
# deploy sets planner_model/injector_model to the install engine's thinker, e.g.
# terra on a Codex-driven install). Effort is fixed regardless of model.
PLANNER_MODEL, PLANNER_EFFORT = "opus", "high"
INJECTOR_MODEL, INJECTOR_EFFORT = "opus", "high"
WORKSTREAM_MODEL, WORKSTREAM_EFFORT = "fable", "xhigh"

Notify = Callable[[object], Awaitable[None]]


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32].strip("-") or "stint"


def _title(plan_text: str) -> str:
    # Hard contract with role-stint-plan: the file's first line is "Stint: <title>".
    return plan_text.splitlines()[0].removeprefix("Stint:").strip()


@dataclass
class _Workstream:
    session: CCSession
    title: str
    vitals: object  # transcript.SessionVitals or rollout.RolloutVitals, per engine
    model: str
    engine: str
    status: str = "running"


class WorkstreamManager:
    def __init__(
        self,
        substrate: Substrate,
        store: Store,
        client_notify: Notify,
        *,
        convo_transcript: Path,
        data_dir: Path,
        plugin_dir: Path,
        settings_file: Path,
        poll_interval: float = 2.0,
        poll_budget: float = 300.0,
        push_interval: float = 5.0,
    ) -> None:
        self.substrate = substrate
        self.store = store
        self.notify = client_notify
        self.convo_transcript = convo_transcript  # runtime re-points this on Clear
        self._data_dir = data_dir
        self._plugin_dir = plugin_dir
        self._settings_file = settings_file
        self._poll_interval = poll_interval
        self._poll_budget = poll_budget
        self._push_interval = push_interval
        self._convo_vitals = self._vitals_for_convo()
        self._workstreams: dict[str, _Workstream] = {}
        for row in store.list_workstreams():
            transcript = (
                substrate.codex_transcript(row.cc_session_id)
                if row.engine == "codex"
                else substrate.transcript_dir / f"{row.cc_session_id}.jsonl"
            )
            spec = self._workstream_spec(row.name, row.title, row.model)
            session = CCSession(row.cc_session_id, row.window, transcript, spec)
            self._workstreams[row.name] = _Workstream(
                session=session, title=row.title, vitals=engines.vitals(transcript, row.engine),
                model=row.model, engine=row.engine, status=row.status,
            )

    def transcript_path(self, name: str) -> Path | None:
        ws = self._workstreams.get(name)
        return ws.session.transcript if ws is not None else None

    async def new_workstream(self) -> None:
        """Plan a stint from the conversation delta, then launch it — the planner's
        output is trusted, never shown for review."""
        since = self.store.get_marker()
        self.store.set_marker(self._convo_lines())
        plan_id = uuid.uuid4().hex[:8]
        output = self._data_dir / "plans" / f"plan-{plan_id}.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        spec = self._passthrough_spec(
            "plan",
            "role-stint-plan",
            self.planner_model(),
            PLANNER_EFFORT,
            f"convo={self.convo_transcript} since_line={since} output={output}",
        )
        planner = await self.substrate.spawn(spec)
        text = await self._await_file(output)
        await self.substrate.kill(planner)
        if text is None:
            await self.notify(protocol.Error(message="stint planner timed out"))
            return
        title = _title(text)
        name = f"ws-{_slug(title)}"
        log(logger, "stint_planned", plan_id=plan_id, title=title)
        model = self.workstream_model()
        engine = engines.engine_of(model)
        session = await self.substrate.spawn(self._workstream_spec(name, title, model))
        if not await self._await_ready(session):
            await self.substrate.kill(session)
            await self.notify(protocol.Error(message="workstream session failed to start"))
            return
        await self.substrate.send(session, text)  # the full plan is the first message
        self.store.add_workstream(
            name, session.session_id, session.window, title, str(output), model, engine
        )
        self._workstreams[name] = _Workstream(
            session=session, title=title, vitals=engines.vitals(session.transcript, engine),
            model=model, engine=engine,
        )
        log(logger, "workstream_launched", name=name, cc_session_id=session.session_id)
        await self.push_cards()

    async def end_workstream(self, name: str) -> None:
        ws = self._workstreams.pop(name, None)
        if ws is None:
            await self.notify(protocol.Error(message=f"unknown workstream: {name}"))
            return
        await self.substrate.kill(ws.session)
        self.store.remove_workstream(name)
        log(logger, "workstream_ended", name=name)
        await self.push_cards()

    async def send_to_workstream(self, name: str) -> None:
        ws = self._workstreams.get(name)
        if ws is None:
            await self.notify(protocol.Error(message=f"unknown workstream: {name}"))
            return
        since = self.store.get_marker()
        current = self._convo_lines()
        output = self._data_dir / "injects" / f"inject-{uuid.uuid4().hex[:8]}.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        spec = self._passthrough_spec(
            "inject",
            "role-inject",
            self.injector_model(),
            INJECTOR_EFFORT,
            f"convo={self.convo_transcript} since_line={since}"
            f" workstream={ws.session.transcript} output={output}",
        )
        session = await self.substrate.spawn(spec)
        directive = await self._await_file(output)
        await self.substrate.kill(session)
        if directive is None:
            await self.notify(protocol.Error(message="injector timed out"))
            return
        await self.substrate.send(ws.session, directive)
        self.store.set_marker(current)
        log(logger, "workstream_injected", name=name)

    async def compact_workstream(self, name: str) -> None:
        ws = self._workstreams.get(name)
        if ws is None:
            await self.notify(protocol.Error(message=f"unknown workstream: {name}"))
            return
        await self.substrate.slash(ws.session, "/compact")
        log(logger, "workstream_compacted", name=name)

    async def run(self) -> None:
        """Push cards every push_interval — even with no workstreams, the message
        carries the convo session's context fill for the Compact button."""
        while True:
            await asyncio.sleep(self._push_interval)
            await self.push_cards()

    async def push_cards(self) -> None:
        if self._convo_vitals.path != self.convo_transcript:  # Clear re-pointed it
            self._convo_vitals = self._vitals_for_convo()
        self._convo_vitals.refresh()
        cards = []
        for name, ws in self._workstreams.items():
            status = "running" if await self.substrate.alive(ws.session) else "gone"
            if status != ws.status:
                ws.status = status
                self.store.set_workstream_status(name, status)
            ws.vitals.refresh()
            cards.append(
                protocol.WorkstreamCard(
                    name=name,
                    title=ws.title,
                    status=status,
                    state=ws.vitals.state,
                    agents=ws.vitals.active_agents,
                    context_pct=ws.vitals.context_pct,
                    model=ws.model,
                    engine=ws.engine,
                )
            )
        await self.notify(
            protocol.Workstreams(
                workstreams=cards,
                convo_context_pct=self._convo_vitals.context_pct,
                convo_model=self.convo_model(),
                workstream_model=self.workstream_model(),
                models=self.enabled_models(),
            )
        )

    def convo_model(self) -> str:
        return self.store.get_setting("convo_model") or CONVO_MODEL

    def workstream_model(self) -> str:
        return self.store.get_setting("workstream_model") or WORKSTREAM_MODEL

    def planner_model(self) -> str:
        return self.store.get_setting("planner_model") or PLANNER_MODEL

    def injector_model(self) -> str:
        return self.store.get_setting("injector_model") or INJECTOR_MODEL

    def enabled_models(self) -> list[str]:
        """The models the phone's picker offers: only engines wired on this box.
        The deploy skill sets the `engines` setting; unset means both."""
        names = (self.store.get_setting("engines") or "claude codex").split()
        return [m for m in engines.MODELS if engines.engine_of(m) in names]

    def set_model(self, target: str, model: str) -> None:
        self.store.set_setting(f"{target}_model", model)
        log(logger, "model_set", target=target, model=model)

    def _passthrough_spec(
        self, name: str, skill: str, model: str, effort: str, args: str
    ) -> SessionSpec:
        """An ephemeral planner/injector spec. Claude gets the skill from the
        plugin dir; codex finds it in ~/.codex/skills (deploy wires the symlinks)."""
        if engines.engine_of(model) == "codex":
            return SessionSpec(
                name=name,
                model=model,
                effort=effort,
                display_name=name,
                engine="codex",
                initial_prompt=f"${skill} {args}",
            )
        return SessionSpec(
            name=name,
            model=model,
            effort=effort,
            display_name=name,
            plugin_dir=self._plugin_dir,
            initial_prompt=f"/remote-workstreams:{skill} {args}",
        )

    def _workstream_spec(self, name: str, title: str, model: str) -> SessionSpec:
        if engines.engine_of(model) == "codex":
            # No settings file: the phone-approval relay is a Claude Code hook;
            # codex workstreams run sandboxed instead (see substrate).
            return SessionSpec(
                name=name,
                model=model,
                effort=WORKSTREAM_EFFORT,
                display_name=title,
                engine="codex",
                initial_prompt="$role-root",
            )
        return SessionSpec(
            name=name,
            model=model,
            effort=WORKSTREAM_EFFORT,
            display_name=title,
            settings_file=self._settings_file,
            initial_prompt="/role-root",
            remote_control=True,  # workstreams show up in the iOS Claude app too
        )

    def _vitals_for_convo(self):
        stored = self.store.get_convo_session()
        return engines.vitals(self.convo_transcript, stored.engine if stored else "claude")

    def _convo_lines(self) -> int:
        try:
            return self.convo_transcript.read_bytes().count(b"\n")
        except FileNotFoundError:
            return 0

    async def _await_file(self, path: Path) -> str | None:
        deadline = time.monotonic() + self._poll_budget
        while True:
            # The file can exist before its content lands; wait for a non-empty read.
            if path.exists() and (text := path.read_text()).strip():
                return text
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_interval)

    async def _await_ready(self, session: CCSession) -> bool:
        """A fresh session swallows pastes until its TUI is up; the role skill's
        greeting landing in the transcript is the ready signal."""
        tail = engines.tail(session.transcript, session.spec.engine)
        deadline = time.monotonic() + self._poll_budget
        while True:
            if any(isinstance(entry, AssistantText) for entry in tail.read_new()):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._poll_interval)
