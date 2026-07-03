"""The audio pipeline state machine. Barge-in is a first-class transition
(SPEAKING → INTERRUPTED), not a retrofit. These states are protocol-visible
(protocol.State), so they are frozen.
"""

from __future__ import annotations

from enum import Enum


class PipelineState(str, Enum):
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"


TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    # endpoint committed a user turn
    PipelineState.LISTENING: frozenset({PipelineState.THINKING}),
    # first TTS audio ready, or the turn produced nothing to say
    PipelineState.THINKING: frozenset({PipelineState.SPEAKING, PipelineState.LISTENING}),
    # utterance finished, or user spoke over it (barge-in)
    PipelineState.SPEAKING: frozenset({PipelineState.LISTENING, PipelineState.INTERRUPTED}),
    # barge-in speech endpointed, or turned out to be nothing
    PipelineState.INTERRUPTED: frozenset({PipelineState.THINKING, PipelineState.LISTENING}),
}


class IllegalTransition(Exception):
    pass


class StateMachine:
    def __init__(self) -> None:
        self.state = PipelineState.LISTENING

    def to(self, new: PipelineState) -> PipelineState:
        if new not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {new.value}")
        self.state = new
        return new
