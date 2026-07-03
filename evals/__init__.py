"""Bridge coherence evals — scripted conversations against the real ConversationEngine.

Run: `uv run python -m evals.bridge` (mock model client, no network).
Live: `uv run python -m evals.bridge --live` (requires VOICECODE_LIVE_EVALS=1 and
Anthropic credentials; Ryan runs these).
"""
