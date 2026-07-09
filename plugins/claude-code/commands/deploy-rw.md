---
description: Deploy remote-workstreams on this Mac — tmux + Tailscale, Deepgram/Cartesia keys into the Keychain, pairing PIN, launchd service, tailscale serve, pairing QR, round-trip test.
---

Read ${CLAUDE_PLUGIN_ROOT}/skills/deploy-rw/SKILL.md and follow it step by step. Its helper
scripts are at ${CLAUDE_PLUGIN_ROOT}/skills/deploy-rw/scripts/.

If the user passed arguments, treat them as the focus (e.g. "repair", "rotate keys",
"re-pair") and jump to the relevant steps after Step 0; otherwise run the full flow.

$ARGUMENTS
