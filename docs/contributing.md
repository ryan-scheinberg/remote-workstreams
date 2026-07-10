# Contributing

Contributions are welcome, especially adapter reliability, Mac/iPhone QA, accessibility, and documentation. Keep changes small enough to review and preserve the provider-neutral contracts.

## Workflow

1. Fork the repository or use a branch in the shared fork. Sync `main` with `upstream/main`.
2. Create a focused branch such as `codex/local-voice-fix` or `feat/pairing-timeout`.
3. Install with `uv sync --extra local-voice`, then make the smallest change that solves the issue.
4. Add or update tests. Provider tests must use fakes; live model tests are opt-in and must not require API keys.
5. Run `uv run ruff check remote_workstreams tests`, `uv run python -m pytest`, and the local round trip when voice code changed.
6. Update the relevant page under `docs/` and the root README if behavior or setup changed.
7. Commit with a clear imperative message. Do not include `.venv`, model caches, SQLite files, transcripts, Keychain exports, or secrets.

## Pull requests

Open a PR against `ryan-scheinberg/remote-workstreams`'s `main` branch. Describe the user-visible result, files changed, test commands and results, platform/model details for voice work, and any follow-up risk. Link an issue when one exists. Include screenshots or a short recording for PWA changes, and state explicitly when a live provider or iPhone test was not possible.

Maintainers will check the full test suite, Ruff, docs drift, protocol compatibility, cancellation/barge-in behavior, and secret hygiene. Keep the PR mergeable: rebase or merge current `upstream/main`, resolve conflicts locally, and update the test evidence before asking for review.
