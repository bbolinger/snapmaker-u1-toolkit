#!/bin/bash
# install_hermes_cancel_hook.sh — install the U1 grace-cancel Gateway
# hook into a Hermes install.
#
# Usage:
#   ./tools/install_hermes_cancel_hook.sh                     # in-container
#   docker exec <hermes-container> bash /path/to/this/script  # from host
#
# What it does:
#   1. Discovers Hermes' actual HOOKS_DIR by importing gateway.hooks.
#      (The docs say ~/.hermes/hooks/ but the real path can differ per
#      install — some containers use
#      /opt/data/hooks. Doing the discovery avoids the papercut of
#      installing into the wrong dir.)
#   2. Copies HOOK.yaml + handler.py into HOOKS_DIR/u1_grace_cancel/.
#   3. Chowns them to the Hermes runtime uid (default 10000 for this
#      containerized install; override via HERMES_UID env var).
#   4. Restarts the gateway so the hook is discovered.
#   5. Verifies the hook loaded (grep gateway.log for `hook(s) loaded`).
#
# Idempotent — safe to run multiple times. Prints the install target
# so you know where the files went.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="${HERE}/hermes_hooks/u1_grace_cancel"
HERMES_PY="${HERMES_PY:-/opt/hermes/.venv/bin/python}"
HERMES_BIN="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"
HERMES_UID="${HERMES_UID:-10000}"
HERMES_GID="${HERMES_GID:-10000}"

if [[ ! -d "${HOOK_SRC}" ]]; then
    echo "install: hook source not found at ${HOOK_SRC}" >&2
    exit 1
fi

# Discover the actual HOOKS_DIR from Hermes itself.
HOOKS_DIR="$("${HERMES_PY}" -c 'from gateway.hooks import HOOKS_DIR; print(HOOKS_DIR)' 2>/dev/null || true)"
if [[ -z "${HOOKS_DIR}" ]]; then
    echo "install: could not resolve HOOKS_DIR via ${HERMES_PY}" >&2
    echo "install: is Hermes installed at that path? Override with HERMES_PY=/path/to/python" >&2
    exit 2
fi

DEST="${HOOKS_DIR}/u1_grace_cancel"
echo "install: HOOKS_DIR = ${HOOKS_DIR}"
echo "install: installing to ${DEST}"

mkdir -p "${DEST}"
cp "${HOOK_SRC}/HOOK.yaml" "${HOOK_SRC}/handler.py" "${DEST}/"

# Chown so the Hermes runtime uid can read them. Skip silently if the
# caller isn't root and can't chown — the copy already worked.
chown "${HERMES_UID}:${HERMES_GID}" "${DEST}/HOOK.yaml" "${DEST}/handler.py" 2>/dev/null || true

# Restart the gateway to pick up the new hook.
echo "install: restarting gateway..."
"${HERMES_BIN}" gateway restart || {
    echo "install: gateway restart returned non-zero — check hermes gateway status" >&2
    exit 3
}

# Give it a beat to come up, then verify THIS hook specifically loaded —
# a generic 'hook(s) loaded' line passes even when u1_grace_cancel failed.
sleep 4
GATEWAY_LOG="${HERMES_HOME:-/opt/data}/logs/gateway.log"
VERIFIED=0
if [[ -r "${GATEWAY_LOG}" ]]; then
    if tail -200 "${GATEWAY_LOG}" | grep -q 'u1_grace_cancel'; then
        echo "install: verified — u1_grace_cancel appears in gateway.log"
        VERIFIED=1
    else
        echo "install: WARNING — u1_grace_cancel not found in the last 200 lines of ${GATEWAY_LOG}." >&2
        echo "install: the hook may not have loaded; reply-CANCEL will silently do nothing until it does." >&2
    fi
else
    echo "install: gateway.log not readable at ${GATEWAY_LOG}, skipping verification" >&2
fi

# Receipt: the notify script checks this file to decide whether the DM may
# honestly promise reply-to-cancel. Only written on verified load.
RECEIPT="${U1_CANCEL_HOOK_RECEIPT:-${HERMES_HOME:-/opt/data}/.u1_cancel_hook_receipt}"
if [[ "${VERIFIED}" == "1" ]]; then
    printf '{"hook": "u1_grace_cancel", "dest": "%s", "installed_at": "%s"}\n' \
        "${DEST}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${RECEIPT}"
    echo "install: receipt written to ${RECEIPT}"
else
    rm -f "${RECEIPT}" 2>/dev/null || true
    echo "install: NO receipt written — grace-period DMs will advertise the SSH fallback instead of reply-CANCEL." >&2
fi

echo "install: done. Cancel any grace-window print by replying CANCEL (all windows)"
echo "install: or 'cancel <code>' (just that request; code = last 6 chars of the request id)."
