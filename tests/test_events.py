import pydantic
import pytest

from voicecode.events import Completed, NeedsApproval, parse_event


def test_event_roundtrip():
    event = NeedsApproval(
        summary="Claude wants to run the test suite.",
        gate_id="g1",
        tool_name="Bash",
        detail="uv run pytest",
    )
    parsed = parse_event(event.model_dump_json())
    assert parsed == event


def test_discriminator_selects_type():
    parsed = parse_event({"type": "completed", "summary": "Done with the auth module."})
    assert isinstance(parsed, Completed)
    assert parsed.id and parsed.ts > 0


def test_unknown_type_rejected():
    with pytest.raises(pydantic.ValidationError):
        parse_event({"type": "telepathy", "summary": "nope"})
