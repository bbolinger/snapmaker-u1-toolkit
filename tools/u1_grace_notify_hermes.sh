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
# /tmp/u1_pending_cancel/<request_id>.json. The Hermes gateway hook at
# tools/hermes_hooks/u1_grace_cancel/ watches that dir and reacts to
# messages of the form `cancel <code>` (where <code> is the last 6
# chars of the request_id). The gate cleans up its own state file
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

set -euo pipefail

HERMES_BIN="${HERMES_BIN:-hermes}"
DEST="${U1_GRACE_NOTIFY_DEST:-telegram}"
PENDING_DIR="/tmp/u1_pending_cancel"

# ISO timestamp `now + grace_seconds + 60` — the +60 is slack for
# clock skew; expired entries are ignored by the hook so a crashed
# gate can't leave a permanent phantom.
if command -v python3 >/dev/null 2>&1; then
    EXPIRES_AT="$(python3 -c "from datetime import datetime, timezone, timedelta; import os; print((datetime.now(timezone.utc) + timedelta(seconds=int(os.environ['U1_GRACE_SECONDS']) + 60)).isoformat())")"
else
    EXPIRES_AT=""
fi

read -r -d '' MSG <<EOF || true
⚠️ Snapmaker U1 print starting in ${U1_GRACE_SECONDS}s

File:     ${U1_FILENAME}
Request:  ${U1_REQUEST_ID}

Reply **CANCEL** to abort. Ignore this to let the print start.
EOF

# Send FIRST. Only persist state on send success.
if ! "${HERMES_BIN}" send --to "${DEST}" "${MSG}"; then
    echo "grace-notify: hermes send failed, skipping pending state write" >&2
    exit 1
fi

# Write the per-request pending state file the hook consumes.
mkdir -p "${PENDING_DIR}"
STATE_FILE="${PENDING_DIR}/${U1_REQUEST_ID}.json"
cat > "${STATE_FILE}" <<EOF
{
  "request_id":     "${U1_REQUEST_ID}",
  "cancel_marker":  "${U1_CANCEL_MARKER}",
  "filename":       "${U1_FILENAME}",
  "grace_seconds":  ${U1_GRACE_SECONDS},
  "operator":       "${U1_OPERATOR}",
  "expires_at":     "${EXPIRES_AT}"
}
EOF

exit 0
