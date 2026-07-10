# Infrastructure

The production shape is deliberately small: one always-on Mac, one launchd user agent, one Tailscale node, and one optional iPhone client. `deploy/com.remote-workstreams.server.plist.template` is rendered to `~/Library/LaunchAgents/com.remote-workstreams.server.plist`.

The service runs the repository's locked dependencies with `uv`, starts the `voice` tmux session, and exposes `127.0.0.1:8400` through Tailscale Serve. Keep the service checkout outside macOS-protected folders such as `~/Documents`; the development checkout can remain anywhere. No Docker, cloud database, public DNS record, or inbound router port is required.

The supported install/repair entrypoint is `skills/deploy-rw/scripts/install_service.sh`. `skills/deploy-rw/scripts/check.sh` is read-only and reports prerequisites, providers, model cache, service state, Tailscale state, and tmux state.
