#!/bin/bash
# u1_grace_notify_hermes.sh — sample U1_GRACE_NOTIFY_CMD script for Hermes users.
#
# Point U1_GRACE_NOTIFY_CMD at this file, e.g. in your env:
#   export U1_GRACE_NOTIFY_CMD=/opt/data/workspaces/snapmaker-u1-toolkit/tools/u1_grace_notify_hermes.sh
#
# The gate exports the following env vars before invoking:
#   U1_REQUEST_ID     — request id (e.g. u1_2026_0701_abc123)
#   U1_FILENAME       — printer storage filename of the plate about to start
#   U1_GRACE_SECONDS  — how long the window is (default 120)
#   U1_CANCEL_MARKER  — absolute path to touch on cancel
#   U1_OPERATOR       — resolved operator identity (e.g. telegram:brent)
#
# Delivery is via `hermes send`, which reuses your existing Hermes gateway
# credentials (Telegram/Discord/Slack/Signal). No LLM, no agent loop, no
# running gateway required for bot-token platforms. Falls back cleanly if
# `hermes` isn't on PATH — the gate audits the failure and still runs the
# grace window (you'd cancel via SSH-touch in that case).
#
# Change `--to telegram` to your preferred platform+chat.

set -eu

HERMES_BIN="${HERMES_BIN:-hermes}"
DEST="${U1_GRACE_NOTIFY_DEST:-telegram}"

# Write the pending-cancel state file that the Hermes gateway hook
# (tools/hermes_hooks/u1_grace_cancel/) watches. When you reply CANCEL
# in the DM, the hook reads this file and touches the marker.
mkdir -p "$(dirname /tmp/u1_pending_cancel_marker)"
cat > /tmp/u1_pending_cancel_marker <<EOF
{
  "request_id": "${U1_REQUEST_ID}",
  "cancel_marker": "${U1_CANCEL_MARKER}",
  "filename": "${U1_FILENAME}",
  "grace_seconds": ${U1_GRACE_SECONDS}
}
EOF

read -r -d '' MSG <<EOF || true
⚠️ Snapmaker U1 print starting in ${U1_GRACE_SECONDS}s

File:     ${U1_FILENAME}
Request:  ${U1_REQUEST_ID}
Operator: ${U1_OPERATOR}

Reply **CANCEL** to abort. Ignore this message to let it start.
EOF

exec "${HERMES_BIN}" send --to "${DEST}" "${MSG}"
