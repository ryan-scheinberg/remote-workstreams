from voicecode.adapters.claude_code_distill import (
    Distiller,
    describe_gate,
    describe_task,
    describe_tool,
)
from voicecode.events import Completed, ErrorEvent, Finding, Progress


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_bash_test_suite_summary():
    summary, detail = describe_tool("Bash", {"command": "uv run pytest tests/ -x"})
    assert summary == "Running the test suite."
    assert detail == "uv run pytest tests/ -x"


def test_bash_skips_wrappers_and_env_assignments():
    summary, _ = describe_tool("Bash", {"command": "FOO=1 uv run ruff check ."})
    assert summary == "Running ruff check."


def test_bash_two_word_command():
    summary, _ = describe_tool("Bash", {"command": "git commit -m 'wip'"})
    assert summary == "Running git commit."


def test_bash_flag_second_token_not_included():
    summary, _ = describe_tool("Bash", {"command": "ls -la /tmp"})
    assert summary == "Running ls."


def test_read_uses_basename_not_full_path():
    summary, detail = describe_tool("Read", {"file_path": "/Users/ryan/app/voicecode/auth.py"})
    assert summary == "Reading auth.py."
    assert detail == "/Users/ryan/app/voicecode/auth.py"


def test_grep_regex_soup_stays_generic():
    summary, _ = describe_tool("Grep", {"pattern": r"^def\s+\w+\(.*\)\s*->"})
    assert summary == "Searching the code."


def test_grep_simple_pattern_is_spoken():
    summary, _ = describe_tool("Grep", {"pattern": "session_id"})
    assert summary == "Searching the code for session_id."


def test_task_uses_description():
    summary, _ = describe_tool("Task", {"description": "audit auth flows", "prompt": "..."})
    assert summary == "Delegating audit auth flows to a subagent."


def test_webfetch_names_host():
    summary, _ = describe_tool("WebFetch", {"url": "https://docs.python.org/3/library/"})
    assert summary == "Fetching a page from docs.python.org."


def test_unknown_mcp_tool_humanized():
    summary, _ = describe_tool("mcp__github__create_pull_request", {"title": "x"})
    assert summary == "Using the create pull request tool."


def test_gate_summary_and_exact_detail():
    summary, detail = describe_gate("Bash", {"command": "rm -rf build/"})
    assert summary.startswith("Approval needed:")
    assert "rm -rf build/" in detail


def test_gate_title_prepended_to_detail():
    _, detail = describe_gate(
        "Write", {"file_path": "/tmp/x", "content": "hi"}, title="Claude wants to write /tmp/x"
    )
    assert detail.startswith("Claude wants to write /tmp/x\n")
    assert '"content": "hi"' in detail


def test_describe_task_trims_to_first_sentence():
    summary = describe_task("Fix the login bug. Then also refactor everything else too.")
    assert summary == "Starting on: Fix the login bug."


def test_debounce_collapses_same_tool_within_window():
    clock = FakeClock()
    distiller = Distiller(window=3.0, clock=clock)
    assert isinstance(distiller.tool_use("Read", {"file_path": "/a.py"}), Progress)
    clock.now = 1.0
    assert distiller.tool_use("Read", {"file_path": "/b.py"}) is None
    clock.now = 2.0
    assert distiller.tool_use("Read", {"file_path": "/c.py"}) is None


def test_debounce_emits_for_different_tool_and_after_window():
    clock = FakeClock()
    distiller = Distiller(window=3.0, clock=clock)
    distiller.tool_use("Read", {"file_path": "/a.py"})
    clock.now = 1.0
    assert isinstance(distiller.tool_use("Bash", {"command": "ls"}), Progress)
    clock.now = 10.0
    assert isinstance(distiller.tool_use("Bash", {"command": "ls"}), Progress)


def test_debounce_window_slides_with_repeats():
    clock = FakeClock()
    distiller = Distiller(window=3.0, clock=clock)
    distiller.tool_use("Read", {"file_path": "/a.py"})
    clock.now = 2.0
    assert distiller.tool_use("Read", {"file_path": "/b.py"}) is None
    clock.now = 4.0  # within 3s of the *previous* repeat
    assert distiller.tool_use("Read", {"file_path": "/c.py"}) is None


def test_turn_result_resets_debounce():
    clock = FakeClock()
    distiller = Distiller(window=3.0, clock=clock)
    distiller.tool_use("Read", {"file_path": "/a.py"})
    distiller.turn_result("done", is_error=False)
    assert isinstance(distiller.tool_use("Read", {"file_path": "/b.py"}), Progress)


def test_short_text_is_not_a_finding():
    assert Distiller().assistant_text("On it.") is None


def test_finding_summary_is_speakable_and_detail_full():
    text = (
        "The `login()` handler in **auth.py** never checks token expiry, so stale "
        "sessions stay valid forever. We should add an expiry check before lookup."
    )
    finding = Distiller().assistant_text(text)
    assert isinstance(finding, Finding)
    assert "`" not in finding.summary and "*" not in finding.summary
    assert finding.summary.endswith(".")
    assert len(finding.summary) <= 141
    assert "expiry check before lookup" in (finding.detail or "")


def test_finding_summary_capped_at_140():
    finding = Distiller().assistant_text("word " * 100)
    assert finding is not None
    assert len(finding.summary) <= 141  # 140 + closing period after ellipsis trim


def test_turn_result_completed_and_error():
    completed = Distiller().turn_result("All 42 tests pass. Coverage is 91%.", is_error=False)
    assert isinstance(completed, Completed)
    assert completed.summary == "All 42 tests pass."
    error = Distiller().turn_result(None, is_error=True)
    assert isinstance(error, ErrorEvent)
    assert error.summary == "The task failed."


def test_turn_result_without_text():
    completed = Distiller().turn_result(None, is_error=False)
    assert completed.summary == "Finished the task."
    assert completed.detail is None
