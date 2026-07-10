# Operations

Useful read-only checks:

```bash
skills/deploy-rw/scripts/check.sh "$PWD"
curl -fsS http://127.0.0.1:8400/healthz
tmux ls
launchctl print gui/$(id -u)/com.remote-workstreams.server
```

Inspect the `StandardOutPath` and `StandardErrorPath` printed by the plist when health checks fail. Attach to the live agent session with `tmux attach -t voice`; disconnecting does not stop it. Use the phone's hush control when you need chat without speaker output.

Before upgrades, keep the working branch and model cache. After code or dependency changes, re-run `uv sync`, the full test suite, the local round trip, and `install_service.sh`. Roll back by booting out launchd, checking out the previous known-good revision, and reinstalling.
