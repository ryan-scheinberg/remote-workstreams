import pytest

from remote_workstreams.audio.state import IllegalTransition, PipelineState, StateMachine


def test_full_turn_cycle():
    sm = StateMachine()
    sm.to(PipelineState.THINKING)
    sm.to(PipelineState.SPEAKING)
    assert sm.to(PipelineState.LISTENING) == PipelineState.LISTENING


def test_barge_in_path():
    sm = StateMachine()
    sm.to(PipelineState.THINKING)
    sm.to(PipelineState.SPEAKING)
    sm.to(PipelineState.INTERRUPTED)
    sm.to(PipelineState.THINKING)


def test_illegal_transition_raises():
    sm = StateMachine()
    with pytest.raises(IllegalTransition):
        sm.to(PipelineState.SPEAKING)  # can't speak straight from listening
