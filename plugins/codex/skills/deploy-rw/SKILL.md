---
name: deploy-rw
description: Deploy remote-workstreams on this Mac — tmux + Tailscale checks, engine wiring (Claude Code / Codex), Deepgram/Cartesia keys into the macOS Keychain, pairing PIN, launchd install, tailscale serve, pairing QR, round-trip test. Use when the user wants to install, deploy, or repair a remote-workstreams service.
---

# Deploy remote-workstreams

You are deploying remote-workstreams: a persistent launchd service on this Mac, reached from the
user's iPhone over their tailnet. The end state is a running service, secrets in the
Keychain, HTTPS on the Mac's MagicDNS name, and a phone paired via QR code. This skill
runs the same from Claude Code or Codex — one deploy serves both engines.

Three helper scripts live in `scripts/` next to this file. They print `key=value` lines
and are all idempotent — re-running any of them is safe.

## Rules

- **Confirm before changing the system.** Before every command that touches system state
  — `git clone`, Keychain writes, `launchctl`, `tailscale serve`, installing Tailscale —
  tell the user exactly what you will run and why, and get a yes. Read-only commands
  (`check.sh`, `tailscale status`, `--help`, `curl` of healthz) need no confirmation.
- **Secrets:** never echo stored secrets back, never write the plaintext PIN anywhere.
- **Re-runs are normal.** This flow doubles as repair: `check.sh` shows what is already
  done; skip completed steps unless the user wants to redo one (e.g. rotate a key).

## Step 0 — Assess

Run `scripts/check.sh [REPO_DIR]` (default checks `~/remote-workstreams`). Read the output and
tell the user what is already in place and which steps remain. On a healthy install
(everything `ok`/`present`/`configured`), say so and ask what they want to change.

## Step 1 — Preflight: macOS, uv, tmux, service repo

- `os=unsupported` → stop; remote-workstreams runs on macOS only (launchd, Keychain, CoreAudio).
- `uv=missing` → have the user install uv (https://docs.astral.sh/uv/) and re-check.
- `tmux=missing` → tmux is a hard prerequisite: every agent session remote-workstreams
  drives lives in the `voice` tmux session. With the user's OK run `brew install tmux`
  (no Homebrew → https://brew.sh first), then re-run `check.sh` and confirm
  `tmux=`/`tmux_version=` report a binary.
- `repo=missing` → the service needs a durable git clone of remote-workstreams. Ask the user if
  they already have one (re-run `check.sh THEIR_PATH` to verify); otherwise, with their
  OK, clone the canonical remote to `~/remote-workstreams`:

  ```
  git clone https://github.com/ryan-scheinberg/remote-workstreams ~/remote-workstreams
  ```

  Never run the service from the plugin's own marketplace clone — the host CLI replaces
  that directory on plugin updates.

Everywhere below, `$REPO` is the resolved repo path and `$TS` is the tailscale binary
path printed by `check.sh`.

## Step 2 — Engines

`check.sh` reports which agent CLIs exist (`claude=` / `codex=`) and whether Codex's
role-skill symlinks are in place (`codex_role_skills=`). It prefers the ChatGPT app's
bundled CLI when present, avoiding an older Homebrew `codex`; set
`REMOTE_WORKSTREAMS_CODEX_COMMAND` to override that choice. At least one CLI must be
installed and logged in. At runtime the model name carries the engine; three store
settings shape what this box offers:

- `engines` — which engines the phone's picker shows (`claude`, `codex`, or both)
- `planner_model` / `injector_model` — who runs `+ Workstream` and `Send latest`
  (default `opus` on Claude Code; `gpt-5.6-terra` is the Codex equivalent)

Claude Code needs no wiring beyond login — the service hands its sessions the plugin
directory at spawn. Wiring Codex is three symlinks (it discovers skills globally):

```
mkdir -p ~/.codex/skills
for s in role-convo role-stint-plan role-inject; do ln -sfn "$REPO/skills/$s" ~/.codex/skills/$s; done
```

Settings are written with this one-liner shape (add `set_setting` lines as needed;
current model names live in `remote_workstreams/engines.py`):

```
(cd "$REPO" && uv run python -c "
from remote_workstreams.config import Config
from remote_workstreams.server.store import Store
store = Store(Config.load().db_path)
store.set_setting('engines', 'claude codex')")
```

Apply by what's installed:

- **Both CLIs:** ask the user whether to wire the second engine too, so both are
  pickable from the phone. Yes → the symlinks above and `engines` = `claude codex`;
  no → `engines` = the CLI you are running in. If you are running inside Codex, also
  set `planner_model` and `injector_model` to `gpt-5.6-terra` — the engine that installs
  drives the planning; the other stays pickable for conversation and workstreams.
- **Claude Code only:** set `engines` = `claude`. Defaults cover the rest.
- **Codex only:** the symlinks above, then `engines` = `codex`, `planner_model`,
  `injector_model`, `convo_model`, and `workstream_model` = `gpt-5.6-terra` so the
  first boot doesn't try to spawn a missing `claude` binary.

All of it is an easy flip later — re-run this step after installing the other CLI.

## Step 3 — Tailscale

- `tailscale=missing` → guide the install: download the Tailscale app from
  https://tailscale.com/download (or the Mac App Store), open it, and log in to their
  tailnet. Wait for the user to say it's done, then re-run `check.sh`. The app's CLI
  lives at `/Applications/Tailscale.app/Contents/MacOS/Tailscale`; `check.sh` finds it.
- `tailscale_state` must be `Running` — if not, have the user log in / toggle it on.
- Note the `magicdns=` name (e.g. `mymac.tail1234.ts.net`). It is the service's public
  name inside the tailnet; you need it in Steps 7–8. If it's empty, MagicDNS is off —
  the user enables it in the Tailscale admin console under DNS.

## Step 4 — Provider API keys

For each of `deepgram-api-key` and `cartesia-api-key` that `check.sh` reports
`missing` (or that the user wants to rotate): ask the user to paste the key
(consoles: console.deepgram.com, play.cartesia.ai), then with their OK store it:

```
printf '%s' 'PASTED_KEY' | scripts/store_secret.sh deepgram-api-key
```

Same command shape for the other name. Keys go only to the login Keychain (service
`remote-workstreams`, matching `remote_workstreams/keychain.py`) — never into files. There is no model
API key: all model use rides the CLIs' own auth.

## Step 5 — Pairing PIN

Skip if `secret_pin-hash=present` and the user isn't changing it. Changing the PIN does
not invalidate paired devices — it only gates future pairings.

1. Ask the user to choose a 4-digit PIN (verify it matches `^[0-9]{4}$`).
2. With their OK, store the **hash only** (the `--hash` flag runs the server's frozen
   `remote_workstreams.server.auth.hash_secret` — scrypt, salt `voice-code-v1`):

```
printf '%s' 'THE_PIN' | scripts/store_secret.sh pin-hash --hash "$REPO"
```

Note: 5 wrong PIN attempts lock pairing out for 10 minutes (server-side); a service
restart also clears the lockout.

## Step 6 — Install the launchd service

Tell the user this will run `uv sync` in `$REPO`, write
`~/Library/LaunchAgents/com.remote-workstreams.server.plist` (rendered from
`$REPO/deploy/com.remote-workstreams.server.plist.template`), and `launchctl bootstrap` the
service. With their OK:

```
scripts/install_service.sh "$REPO"
```

It fails fast if tmux is missing, then waits up to 30s for
`http://127.0.0.1:8400/healthz`. On `healthz=failed`, read the log files named in the
rendered plist, fix, and re-run the script.

At boot the service does the session bootstrapping itself — nothing to do here, but
verify it took: it writes `~/.remote-workstreams/workstream-settings.json` (per-boot approval
hook wiring), creates the `voice` tmux session if absent, and spawns or resumes
the persistent convo session in window `voice:convo`. A healthy `check.sh`
therefore also shows `tmux_session=voice` and `convo_window=alive`; if either is off
while `healthz=ok`, read the service logs — the tmux bootstrap failed.

## Step 7 — Expose over the tailnet

`tailscale serve` puts HTTPS (real Let's Encrypt cert) on the MagicDNS name and proxies
to the local service. The CLI syntax has changed across versions — run
`"$TS" serve --help` first and adapt:

- Modern form: `"$TS" serve --bg 8400`
- Older form: `"$TS" serve --bg --https=443 http://127.0.0.1:8400`

Confirm with the user, run it, then verify: `"$TS" serve status` shows the mapping, and
`curl -fsS https://MAGICDNS_NAME/healthz` succeeds. If serve complains that HTTPS is
disabled for the tailnet, the user enables "HTTPS Certificates" in the Tailscale admin
console (DNS page) and you retry. First cert issuance can take a minute.

## Step 8 — Pair the phone

Print the pairing URL as a QR code plus plaintext:

```
(cd "$REPO" && uv run python -c "import qrcode; qr = qrcode.QRCode(border=1); qr.add_data('https://MAGICDNS_NAME/'); qr.print_ascii(invert=True)")
```

(Note: build a `QRCode` object — `qrcode.make(...)` returns an image with no
`print_ascii`.) Then walk the user through it:

1. On the iPhone (on the same tailnet, Tailscale app connected), scan the QR or open the
   URL in Safari.
2. Share → **Add to Home Screen**, then open it from the Home Screen.
3. Tap **Pair this device**, enter the 4-digit PIN.
4. Approve the Face ID prompt (WebAuthn registration — the passkey is stored and used
   for every later unlock).

The phone now holds a passkey; every app open is one Face ID tap (Unlock). After a
service upgrade that changed the credential schema, or a server restart, existing
phones just Unlock again — re-pairing is only needed if the server says the passkey
is no longer valid.

## Step 9 — Final check and report

Run the audio round-trip test (synthesized speech in → transcript → reply audio out,
uses the live keys):

```
(cd "$REPO" && uv run python -m remote_workstreams.audio.roundtrip)
```

Report pass/fail. Finish with a summary: service state, tmux session state
(`tmux_session=` / `convo_window=` from a final `check.sh`), engines wired, MagicDNS
URL, pairing status, round-trip result, and the rollback notes below.

## Rollback / uninstall

- Stop the service: `launchctl bootout gui/$(id -u)/com.remote-workstreams.server`
- Roll back code: `git -C "$REPO" checkout PREVIOUS_TAG`, then re-run
  `scripts/install_service.sh "$REPO"`
- Stop serving: `"$TS" serve reset` (or the equivalent shown by `"$TS" serve --help`)
- Remove secrets: `security delete-generic-password -s remote-workstreams -a NAME` per entry
- Unwire Codex: `rm -f ~/.codex/skills/role-convo ~/.codex/skills/role-stint-plan ~/.codex/skills/role-inject`
