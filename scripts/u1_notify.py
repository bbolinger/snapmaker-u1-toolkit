#!/usr/bin/env python3
"""Model-free operator notifications for the U1 start path.

One notifier, three users: the grace countdown DM (with its inline CANCEL
button), the gate's cancellation confirmation, and the confirm-hook's
refusal reports. Exists because live testing (2026-07-07) showed the agent
model happily narrating outcomes it never saw — "cancelled" while the grace
window ran on, "start signal sent" about a refusal it couldn't read. Every
start-path outcome now arrives from the machinery itself, so there is no
silence for a model to confabulate into.

Sends via the Telegram Bot API directly (the only channel that can carry an
inline keyboard); falls back to `hermes send` (text only) when the token or
API is unavailable. Reads TELEGRAM_BOT_TOKEN from the environment or from
the runtime .env; the destination chat comes from the same operator binding
the YES hook enforces.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

_ENV_FILE = Path(os.environ.get("HERMES_HOME", "/opt/data")) / ".env"


def _bot_token() -> str | None:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok.strip()
    try:
        for line in _ENV_FILE.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    except Exception:
        pass
    return None


def _chat_id() -> str | None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from u1_config import get_operator_binding
        binding = get_operator_binding()
        if binding and binding[0] == "telegram":
            return str(binding[1])
    except Exception:
        pass
    return None


def send_operator(text: str,
                  cancel_button_request_id: str | None = None) -> bool:
    """Send *text* to the operator. When *cancel_button_request_id* is set,
    attach a single inline 🛑 CANCEL button whose callback the u1-form
    plugin handles at the adapter layer — the reply-CANCEL path can be
    swallowed by a mid-turn interrupt (live 2026-07-07, twice); a button
    callback cannot. Returns True when a send succeeded on any channel."""
    token = _bot_token()
    chat = _chat_id()
    if token and chat:
        payload: dict[str, Any] = {"chat_id": chat, "text": text}
        if cancel_button_request_id:
            payload["reply_markup"] = {"inline_keyboard": [[{
                "text": "\U0001f6d1 CANCEL this print",
                "callback_data": f"u1c:{cancel_button_request_id}",
            }]]}
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if json.loads(resp.read().decode()).get("ok"):
                    return True
        except Exception as exc:
            print(f"u1_notify: bot api send failed: {exc}", file=sys.stderr)
    # Fallback: hermes send (no button, but the typed-CANCEL hook still works
    # when dispatch is normal).
    try:
        hermes_bin = os.environ.get("HERMES_BIN", "hermes")
        rc = subprocess.run([hermes_bin, "send", "--to",
                             os.environ.get("U1_GRACE_NOTIFY_DEST", "telegram"),
                             text], timeout=20).returncode
        return rc == 0
    except Exception as exc:
        print(f"u1_notify: hermes send fallback failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--cancel-button", default=None, dest="cancel_button",
                    help="request id for an inline CANCEL button")
    a = ap.parse_args()
    ok = send_operator(a.text, cancel_button_request_id=a.cancel_button)
    sys.exit(0 if ok else 1)
