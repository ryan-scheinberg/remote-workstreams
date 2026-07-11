# Operations

Useful read-only checks:

```bash
skills/deploy-rw/scripts/check.sh "$PWD"
curl -fsS http://127.0.0.1:8400/healthz
tmux ls
tmux list-clients
launchctl print gui/$(id -u)/com.remote-workstreams.server
```

Inspect the `StandardOutPath` and `StandardErrorPath` printed by the plist when health checks fail. Attach to the live agent session with `tmux attach -t voice`; disconnecting does not stop it. Use the phone's hush control when you need chat without speaker output.

The service normally owns one invisible `xterm-256color` tmux client per window it has driven. These clients translate real terminal input for modern Claude/Codex TUIs. Do not kill them while a message is being submitted; restarting the launchd service recreates them when needed.

For the user-space Tailscale fallback, point the CLI at its socket:

```bash
/opt/homebrew/bin/tailscale --socket="$HOME/.local/share/tailscale/tailscaled.sock" status
/opt/homebrew/bin/tailscale --socket="$HOME/.local/share/tailscale/tailscaled.sock" serve status
```

The Mac may not be able to curl its own MagicDNS HTTPS name when Tailscale runs in user-space mode. Confirm the Serve mapping locally and verify reachability from the paired phone.

Before upgrades, keep the working branch and model cache. After code or dependency changes, re-run `uv sync`, the full test suite, the local round trip, and `install_service.sh`. Roll back by booting out launchd, checking out the previous known-good revision, and reinstalling.
