#!/bin/bash
# u1_grace_notify_hermes.sh — U1_GRACE_NOTIFY_CMD script for Hermes users.
#
# Point U1_GRACE_NOTIFY_CMD at this file:
#   export U1_GRACE_NOTIFY_CMD=/opt/data/workspaces/snapmaker-u1-toolkit/tools/u1_grace_notify_hermes.sh
#
# The gate exports the following env vars before invoking:
#   U1_REQUEST_ID     — request id (e.g. u1_2026_0701_abc123)
#   U1_FILENAME       — printer storage filename of the plate about to start
#   U1_GRACE_SECONDS  — how long the window is (default 120)
#   U1_CANCEL_MARKER  — absolute path to touch on cancel
#   U1_OPERATOR       — resolved operator identity (e.g. telegram:brent)
#
# Wire: this script writes a per-request pending-cancel state file at
# <pending-cancel dir>/<request_id>.json. The Hermes gateway hook at
# tools/hermes_hooks/u1_grace_cancel/ watches that dir and reacts to a
# bare CANCEL / STOP / ABORT reply (cancels every active window) or
# `cancel <code>` where <code> is the last 6 chars of the request_id
# (cancels only that window). The gate cleans up its own state file
# on ANY exit path (cancel OR expire).
#
# Send-first ordering: we send the Telegram message BEFORE writing the
# state file so that a Telegram failure doesn't leave a phantom
# pending window that a future unrelated cancel could touch. If
# `hermes send` returns non-zero, we skip the state-file write and
# exit non-zero — the gate audits the notify_failed row and still
# runs the grace period silently (SSH-touch is the fallback in that
# case, but see feedback_no_grace_notify_ssh — we consider silent
# grace acceptable if notify infra is down).
#
# Hook honesty: replying CANCEL only works if the gateway hook is
# actually installed. install_hermes_cancel_hook.sh writes a receipt
# file; when it's missing, the DM says so and gives the SSH fallback
# instead of promising a reply-cancel that would silently do nothing.

set -euo pipefail

HERMES_BIN="${HERMES_BIN:-hermes}"
DEST="${U1_GRACE_NOTIFY_DEST:-telegram}"
# Pending-cancel dir: the invoking gate exports U1_PENDING_CANCEL_DIR with
# its own resolution, so both sides of the marker contract always agree.
# The fallback mirrors scripts/u1_pending.py for manual runs only.
if [[ -n "${U1_PENDING_CANCEL_DIR:-}" ]]; then
    PENDING_DIR="${U1_PENDING_CANCEL_DIR}"
elif [[ -n "${U1_PENDING_STATE_DIR:-}" ]]; then
    PENDING_DIR="${U1_PENDING_STATE_DIR}/cancel"
else
    PENDING_DIR="${TMPDIR:-/tmp}/u1_pending/cancel"
fi
HOOK_RECEIPT="${U1_CANCEL_HOOK_RECEIPT:-${HERMES_HOME:-/opt/data}/.u1_cancel_hook_receipt}"

# ISO timestamp `now + grace_seconds + 60` — the +60 is slack for
# clock skew; expired entries are ignored by the hook so a crashed
# gate can't leave a permanent phantom. python3 is guaranteed here:
# the gate that invokes this script is itself python3.
EXPIRES_AT="$(python3 -c "from datetime import datetime, timezone, timedelta; import os; print((datetime.now(timezone.utc) + timedelta(seconds=int(os.environ['U1_GRACE_SECONDS']) + 60)).isoformat())")" || EXPIRES_AT=""

# Last 6 chars of the request id — the code for a scoped `cancel <code>`.
CODE="${U1_REQUEST_ID: -6}"

if [[ -f "${HOOK_RECEIPT}" ]]; then
    CANCEL_LINE="Tap 🛑 CANCEL below, or reply CANCEL. Ignore this to let the print start."
else
    CANCEL_LINE="Tap 🛑 CANCEL below to abort. (Reply-to-cancel hook not detected — run tools/install_hermes_u1_hooks.sh. SSH fallback: touch '${U1_CANCEL_MARKER}')"
fi

read -r -d '' MSG <<EOF || true
⚠️ Snapmaker U1 print starting in ${U1_GRACE_SECONDS}s

File:     ${U1_FILENAME}

${CANCEL_LINE}
EOF

# Send FIRST. Only persist state on send success. u1_notify sends via the
# Bot API with an inline 🛑 CANCEL button (the button callback is handled at
# the gateway adapter layer, immune to the mid-turn-interrupt loss that ate
# typed CANCELs twice on 2026-07-07); it falls back to plain `hermes send`
# internally when the token/API is unavailable.
NOTIFY_PY="${U1_NOTIFY_PY:-/opt/data/scripts/u1_notify.py}"
if ! python3 "${NOTIFY_PY}" "${MSG}" --cancel-button "${U1_REQUEST_ID}"; then
    echo "grace-notify: operator send failed on all channels, skipping pending state write" >&2
    exit 1
fi

# Write the per-request pending state file the hook consumes. Built with
# python3's json module — shell interpolation into a JSON heredoc broke
# on filenames containing quotes/newlines, and a malformed entry is
# silently dropped by the hook (= that request becomes uncancellable).
mkdir -p "${PENDING_DIR}"
EXPIRES_AT="${EXPIRES_AT}" python3 - "${PENDING_DIR}" <<'PYEOF'
import json, os, sys
from pathlib import Path

pending_dir = Path(sys.argv[1])
request_id = os.environ["U1_REQUEST_ID"]
# The request id is internally generated, but it becomes a filename here —
# refuse anything that isn't a plain token rather than write a weird path.
safe = request_id.replace("-", "").replace("_", "")
if not safe.isalnum() or "/" in request_id or request_id in (".", ".."):
    print(f"grace-notify: refusing suspicious request id {request_id!r}",
          file=sys.stderr)
    sys.exit(1)
state = {
    "request_id": request_id,
    "cancel_marker": os.environ["U1_CANCEL_MARKER"],
    "filename": os.environ["U1_FILENAME"],
    "grace_seconds": int(os.environ["U1_GRACE_SECONDS"]),
    "operator": os.environ.get("U1_OPERATOR", ""),
    "expires_at": os.environ.get("EXPIRES_AT", ""),
}
(pending_dir / f"{request_id}.json").write_text(json.dumps(state, indent=2))
PYEOF

exit 0
