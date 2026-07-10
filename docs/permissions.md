# Permissions and trust boundaries

- The FastAPI process binds to loopback by default. Tailscale Serve is the only intended network exposure.
- `/healthz` is unauthenticated for local service checks. Pairing, login, credentials, and the WebSocket require a valid WebAuthn-backed session.
- The pairing PIN is stored as an scrypt hash in the Keychain, never as plaintext. The iPhone stores a WebAuthn passkey and receives short-lived session tokens.
- Claude destructive-command approvals travel through the authenticated socket to the phone. Codex runs in its CLI sandbox and does not use Claude's hook-based approval relay.
- One socket owns the live runtime. A new valid connection takes over; the Mac sessions continue in tmux if Safari disconnects.
