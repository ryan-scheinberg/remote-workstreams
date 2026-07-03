import pytest

from voicecode.engine.dispatch import DispatchFilter

TAGGED = "Okay, on it.<dispatch>do the thing</dispatch>"


def run(deltas: list[str]) -> tuple[str, str | None]:
    fltr = DispatchFilter()
    spoken = "".join(fltr.feed(d) for d in deltas)
    spoken += fltr.flush()
    return spoken, fltr.dispatch


def test_whole_tag_single_delta():
    spoken, dispatch = run([TAGGED])
    assert spoken == "Okay, on it."
    assert dispatch == "do the thing"


@pytest.mark.parametrize("split", range(1, len(TAGGED)))
def test_tag_split_at_every_point(split: int):
    spoken, dispatch = run([TAGGED[:split], TAGGED[split:]])
    assert spoken == "Okay, on it."
    assert dispatch == "do the thing"


def test_three_way_split():
    spoken, dispatch = run(["Sure.<dis", "patch>fix auth</disp", "atch> Done."])
    assert spoken == "Sure. Done."
    assert dispatch == "fix auth"


def test_plain_angle_brackets_pass_through():
    spoken, dispatch = run(["a < b and 3 <4 and <div> too"])
    assert spoken == "a < b and 3 <4 and <div> too"
    assert dispatch is None


def test_partial_open_at_stream_end_is_spoken():
    spoken, dispatch = run(["thinking <dis"])
    assert spoken == "thinking <dis"
    assert dispatch is None


def test_unclosed_dispatch_captured_on_flush():
    spoken, dispatch = run(["Hi.<dispatch>do it"])
    assert spoken == "Hi."
    assert dispatch == "do it"


def test_unclosed_dispatch_with_partial_close_tag():
    spoken, dispatch = run(["Hi.<dispatch>do it</disp"])
    assert spoken == "Hi."
    assert dispatch == "do it"


def test_text_after_tag_still_spoken():
    spoken, dispatch = run(["before.<dispatch>x</dispatch>after."])
    assert spoken == "before.after."
    assert dispatch == "x"


def test_empty_dispatch_is_none():
    spoken, dispatch = run(["Hello.<dispatch></dispatch>"])
    assert spoken == "Hello."
    assert dispatch is None


def test_angle_bracket_inside_dispatch_content():
    spoken, dispatch = run(["Go.<dispatch>ensure a < b holds</dispatch>"])
    assert spoken == "Go."
    assert dispatch == "ensure a < b holds"
