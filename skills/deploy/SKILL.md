---
name: deploy
description: Deploy voice-code on this Mac — Tailscale check, provider keys into the macOS Keychain, pairing token/PIN, launchd install, tailscale serve, pairing QR, round-trip test. Use when the user wants to install, deploy, or repair a voice-code service.
---

# Deploy voice-code

You are deploying voice-code: a persistent launchd service on this Mac, reached from the
user's iPhone over their tailnet. The end state is a running service, secrets in the
Keychain, HTTPS on the Mac's MagicDNS name, and a phone paired via QR code.

Three helper scripts live in `scripts/` next to this file. They print `key=value` lines
and are all idempotent — re-running any of them is safe.

## Rules

- **Confirm before changing the system.** Before every command that touches system state
  — `git clone`, Keychain writes, `launchctl`, `tailscale serve`, installing Tailscale —
  tell the user exactly what you will run and why, and get a yes. Read-only commands
  (`check.sh`, `tailscale status`, `--help`, `curl` of healthz) need no confirmation.
- **Secrets:** never echo stored secrets back, never write plaintext pairing secrets
  anywhere. The pairing token is shown to the user exactly once, in Step 4.
- **Re-runs are normal.** This flow doubles as repair: `check.sh` shows what is already
  done; skip completed steps unless the user wants to redo one (e.g. rotate a key).

## Step 0 — Assess

Run `scripts/check.sh [REPO_DIR]` (default checks `~/voice-code`). Read the output and
tell the user what is already in place and which steps remain. On a healthy install
(everything `ok`/`present`/`configured`), say so and ask what they want to change.

## Step 1 — Preflight: macOS, uv, service repo

- `os=unsupported` → stop; voice-code runs on macOS only (launchd, Keychain, CoreAudio).
- `uv=missing` → have the user install uv (https://docs.astral.sh/uv/) and re-check.
- `repo=missing` → the service needs a durable git clone of voice-code. Ask the user if
  they already have one (re-run `check.sh THEIR_PATH` to verify); otherwise, with their
  OK, clone the canonical remote to `~/voice-code`:

  ```
  git clone https://github.com/ryan-scheinberg/voice-code ~/voice-code
  ```

  Never run the service from the plugin's own marketplace clone — Claude Code replaces
  that directory on plugin updates.

Everywhere below, `$REPO` is the resolved repo path and `$TS` is the tailscale binary
path printed by `check.sh`.

## Step 2 — Tailscale

- `tailscale=missing` → guide the install: download the Tailscale app from
  https://tailscale.com/download (or the Mac App Store), open it, and log in to their
  tailnet. Wait for the user to say it's done, then re-run `check.sh`. The app's CLI
  lives at `/Applications/Tailscale.app/Contents/MacOS/Tailscale`; `check.sh` finds it.
- `tailscale_state` must be `Running` — if not, have the user log in / toggle it on.
- Note the `magicdns=` name (e.g. `mymac.tail1234.ts.net`). It is the service's public
  name inside the tailnet; you need it in Steps 6–7. If it's empty, MagicDNS is off —
  the user enables it in the Tailscale admin console under DNS.

## Step 3 — Provider API keys

For each of `anthropic-api-key`, `deepgram-api-key`, `cartesia-api-key` that `check.sh`
reports `missing` (or that the user wants to rotate): ask the user to paste the key
(consoles: console.anthropic.com, console.deepgram.com, play.cartesia.ai), then with
their OK store it:

```
printf '%s' 'PASTED_KEY' | scripts/store_secret.sh anthropic-api-key
```

Same command shape for the other two names. Keys go only to the login Keychain
(service `voice-code`, matching `voicecode/keychain.py`) — never into files.

## Step 4 — Pairing token and PIN

Skip if both hashes are `present` and the user isn't re-pairing. Re-doing this step
invalidates existing pairings — say so before overwriting.

1. Generate a token: `openssl rand -base64 33 | tr '+/' '-_'` (44 URL-safe chars).
2. Show it to the user **once**, clearly: *"This is your pairing token. You will type it
   on your iPhone in the final step. It is never shown again — keep it until pairing is
   done."*
3. Ask the user to choose a 4-digit PIN (verify it matches `^[0-9]{4}$`).
4. With their OK, store **hashes only** (the `--hash` flag runs the server's frozen
   `voicecode.server.auth.hash_secret` — scrypt, salt `voice-code-v1`):

```
printf '%s' 'THE_TOKEN' | scripts/store_secret.sh pairing-token-hash --hash "$REPO"
printf '%s' 'THE_PIN'   | scripts/store_secret.sh pin-hash           --hash "$REPO"
```

## Step 5 — Install the launchd service

Tell the user this will run `uv sync` in `$REPO`, write
`~/Library/LaunchAgents/com.voicecode.server.plist` (rendered from
`$REPO/deploy/com.voicecode.server.plist.template`), and `launchctl bootstrap` the
service. With their OK:

```
scripts/install_service.sh "$REPO"
```

It waits up to 30s for `http://127.0.0.1:8400/healthz`. On `healthz=failed`, read the
log files named in the rendered plist, fix, and re-run the script.

## Step 6 — Expose over the tailnet

`tailscale serve` puts HTTPS (real Let's Encrypt cert) on the MagicDNS name and proxies
to the local service. The CLI syntax has changed across versions — run
`"$TS" serve --help` first and adapt:

- Modern form: `"$TS" serve --bg 8400`
- Older form: `"$TS" serve --bg --https=443 http://127.0.0.1:8400`

Confirm with the user, run it, then verify: `"$TS" serve status` shows the mapping, and
`curl -fsS https://MAGICDNS_NAME/healthz` succeeds. If serve complains that HTTPS is
disabled for the tailnet, the user enables "HTTPS Certificates" in the Tailscale admin
console (DNS page) and you retry. First cert issuance can take a minute.

## Step 7 — Pair the phone

Print the pairing URL as a QR code plus plaintext:

```
(cd "$REPO" && uv run python -c "import qrcode; qr = qrcode.QRCode(border=1); qr.add_data('https://MAGICDNS_NAME/'); qr.print_ascii(invert=True)")
```

(Note: build a `QRCode` object — `qrcode.make(...)` returns an image with no
`print_ascii`.) Then walk the user through it:

1. On the iPhone (on the same tailnet, Tailscale app connected), scan the QR or open the
   URL in Safari.
2. Share → **Add to Home Screen**, then open it from the Home Screen.
3. Enter the pairing token from Step 4 and the 4-digit PIN.
4. Approve the Face ID prompt (WebAuthn registration).

The phone now holds a long-lived credential; reconnects need no re-auth.

## Step 8 — Final check and report

Run the audio round-trip test (synthesized speech in → transcript → reply audio out,
uses the live keys):

```
(cd "$REPO" && uv run python -m voicecode.audio.roundtrip)
```

Report pass/fail. Finish with a summary: service state, MagicDNS URL, pairing status,
round-trip result, and the rollback notes below.

## Rollback / uninstall

- Stop the service: `launchctl bootout gui/$(id -u)/com.voicecode.server`
- Roll back code: `git -C "$REPO" checkout PREVIOUS_TAG`, then re-run
  `scripts/install_service.sh "$REPO"`
- Stop serving: `"$TS" serve reset` (or the equivalent shown by `"$TS" serve --help`)
- Remove secrets: `security delete-generic-password -s voice-code -a NAME` per entry
