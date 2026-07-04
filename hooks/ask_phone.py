#!/usr/bin/env python3
"""voice-code phone-approval relay: PreToolUse hook JSON on stdin, verdict on stdout.

Two modes:
- default (pure relay): POST the payload to the local voice-code service and wait
  for the phone's allow/deny.
- --gate-bash: only relay Bash commands matching the short destructive list below;
  everything else exits silently and instantly.

Any user hook can exec this script. On timeout, non-200, or unreachable service it
prints nothing and exits 0 — Claude Code's native permission behavior takes over.
"""

import argparse
import json
import re
import sys
import urllib.request

DESTRUCTIVE = [
    r"\bsudo\b",
    r"\brm\s+-\w*r\w*f",
    r"\brm\s+-\w*f\w*r",
    r"\bgit\s+push\b.*(--force\b|\s-f\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+clean\b",
    r"\blaunchctl\b",
    r"\bkill\s+-9\b",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--wait", type=float, default=90)
    parser.add_argument("--gate-bash", action="store_true")
    args = parser.parse_args()
    payload = json.load(sys.stdin)

    if args.gate_bash:
        if payload.get("tool_name") != "Bash":
            return
        command = str((payload.get("tool_input") or {}).get("command", ""))
        if not any(re.search(pattern, command) for pattern in DESTRUCTIVE):
            return

    request = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/approvals",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Voicecode-Token": args.token},
    )
    try:
        with urllib.request.urlopen(request, timeout=args.wait) as response:
            decision = json.load(response).get("decision")
    except Exception:
        return  # silence: Claude Code's native permission prompt takes over
    if decision in ("allow", "deny"):
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": "voice-code phone approval",
            }
        }
        print(json.dumps(output))


if __name__ == "__main__":
    main()
