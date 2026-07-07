#!/bin/bash
# install_hermes_u1_hooks.sh — install BOTH U1 Gateway hooks into a Hermes
# install:
#
#   u1_grace_cancel   reply CANCEL during the pre-start grace window
#   u1_confirm_start  reply YES at the bed-clear prompt (model-free start)
#
# Usage:
#   bash tools/install_hermes_u1_hooks.sh            # install/refresh both
#   bash tools/install_hermes_u1_hooks.sh --verify   # check an existing install
#
# Hooks-dir discovery: HERMES_HOOKS_DIR wins when set; otherwise the script
# imports gateway.hooks with $HERMES_PY (default /opt/hermes/.venv/bin/python)
# and uses Hermes' own HOOKS_DIR. The docs say ~/.hermes/hooks/ but real
# installs differ (this container uses /opt/data/hooks) — asking Hermes
# avoids the papercut of installing into the wrong dir.
#
# Each installed hook gets a receipt at <hooks_dir>/<hook>/.install_receipt.json
# (toolkit version, timestamp, source path, file hashes). --verify checks
# both hook dirs for non-empty handler.py + HOOK.yaml + receipt and exits
# non-zero naming whatever is missing.
#
# This script does NOT restart the gateway — installs can run from deploy
# flows on boxes where the gateway lives elsewhere. It prints the restart
# command and what to look for in the gateway log. Until the gateway
# restarts and loads the hooks, a YES at the bed-clear prompt redeems
# nothing (fail-safe: the printer never starts) and reply-CANCEL does
# nothing during grace windows.
#
# Idempotent — every run refreshes files + receipts.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
HERMES_PY="${HERMES_PY:-/opt/hermes/.venv/bin/python}"
HERMES_BIN="${HERMES_BIN:-/opt/hermes/.venv/bin/hermes}"
HERMES_UID="${HERMES_UID:-10000}"
HERMES_GID="${HERMES_GID:-10000}"
GATEWAY_LOG="${HERMES_HOME:-/opt/data}/logs/gateway.log"
NOTIFY_RECEIPT="${U1_CANCEL_HOOK_RECEIPT:-${HERMES_HOME:-/opt/data}/.u1_cancel_hook_receipt}"
HOOKS=(u1_grace_cancel u1_confirm_start)

MODE="install"
if [[ "${1:-}" == "--verify" ]]; then
    MODE="verify"
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--verify]" >&2
    exit 64
fi

# ---- hooks-dir discovery ---------------------------------------------------
if [[ -n "${HERMES_HOOKS_DIR:-}" ]]; then
    HOOKS_DIR="${HERMES_HOOKS_DIR}"
else
    HOOKS_DIR="$("${HERMES_PY}" -c 'from gateway.hooks import HOOKS_DIR; print(HOOKS_DIR)' 2>/dev/null || true)"
fi
if [[ -z "${HOOKS_DIR}" ]]; then
    echo "hooks: could not resolve the Hermes hooks dir via ${HERMES_PY}" >&2
    echo "hooks: is Hermes installed at that path? Override with HERMES_PY=/path/to/python" >&2
    echo "hooks: or set HERMES_HOOKS_DIR=/path/to/hooks directly" >&2
    exit 2
fi

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        echo "unavailable"
    fi
}

# ---- verify mode -----------------------------------------------------------
if [[ "${MODE}" == "verify" ]]; then
    MISSING=()
    for hook in "${HOOKS[@]}"; do
        dest="${HOOKS_DIR}/${hook}"
        if [[ ! -d "${dest}" ]]; then
            MISSING+=("${hook}: directory ${dest} missing")
            continue
        fi
        [[ -s "${dest}/handler.py" ]] || MISSING+=("${hook}: handler.py missing or empty")
        [[ -s "${dest}/HOOK.yaml" ]] || MISSING+=("${hook}: HOOK.yaml missing or empty")
        [[ -s "${dest}/.install_receipt.json" ]] || MISSING+=("${hook}: .install_receipt.json missing (run tools/install_hermes_u1_hooks.sh)")
    done
    if ((${#MISSING[@]})); then
        echo "verify: FAIL — hooks dir ${HOOKS_DIR}" >&2
        printf 'verify:   %s\n' "${MISSING[@]}" >&2
        echo "verify: fix with: bash ${HERE}/install_hermes_u1_hooks.sh" >&2
        exit 1
    fi
    echo "verify: OK — both hooks present with receipts in ${HOOKS_DIR}"

    # Best-effort load check: files on disk aren't a loaded hook. When the
    # gateway log is readable, look for both hook names and refresh the
    # notify script's honesty receipt (the grace-period DM only promises
    # reply-CANCEL when this receipt exists). Never fails the verify —
    # deploy boxes without a gateway have no log and that's fine.
    if [[ -r "${GATEWAY_LOG}" ]]; then
        LOG_TAIL="$(tail -n 300 "${GATEWAY_LOG}" 2>/dev/null || true)"
        if grep -q 'u1_grace_cancel' <<< "${LOG_TAIL}"; then
            if printf '{"hook": "u1_grace_cancel", "dest": "%s", "installed_at": "%s"}\n' \
                "${HOOKS_DIR}/u1_grace_cancel" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${NOTIFY_RECEIPT}" 2>/dev/null; then
                echo "verify: gateway log shows u1_grace_cancel loaded — notify receipt refreshed at ${NOTIFY_RECEIPT}"
            else
                echo "verify: gateway log shows u1_grace_cancel loaded (could not write ${NOTIFY_RECEIPT})"
            fi
        else
            echo "verify: u1_grace_cancel NOT in the last 300 lines of ${GATEWAY_LOG} — restart the gateway, then re-run --verify" >&2
        fi
        if grep -q 'u1_confirm_start' <<< "${LOG_TAIL}"; then
            echo "verify: gateway log shows u1_confirm_start loaded — operator YES starts the print"
        else
            echo "verify: u1_confirm_start NOT in the last 300 lines of ${GATEWAY_LOG} — until the gateway restarts and loads it, YES does nothing (the printer never starts)" >&2
        fi
    else
        echo "verify: gateway log not readable at ${GATEWAY_LOG} — file checks only (fine on a box that doesn't run the gateway)"
    fi
    exit 0
fi

# ---- install mode ----------------------------------------------------------
TOOLKIT_VERSION="$(git -C "${REPO_ROOT}" describe --tags --always --dirty 2>/dev/null || echo unknown)"
echo "install: hooks dir = ${HOOKS_DIR}"

for hook in "${HOOKS[@]}"; do
    src="${HERE}/hermes_hooks/${hook}"
    if [[ ! -s "${src}/handler.py" || ! -s "${src}/HOOK.yaml" ]]; then
        echo "install: hook source incomplete at ${src}" >&2
        exit 1
    fi
    dest="${HOOKS_DIR}/${hook}"
    mkdir -p "${dest}"
    cp "${src}/HOOK.yaml" "${src}/handler.py" "${dest}/"
    printf '{\n  "hook": "%s",\n  "toolkit_version": "%s",\n  "installed_at": "%s",\n  "source": "%s",\n  "sha256": {\n    "handler.py": "%s",\n    "HOOK.yaml": "%s"\n  }\n}\n' \
        "${hook}" "${TOOLKIT_VERSION}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${src}" \
        "$(_sha256 "${dest}/handler.py")" "$(_sha256 "${dest}/HOOK.yaml")" \
        > "${dest}/.install_receipt.json"
    chmod 644 "${dest}/HOOK.yaml" "${dest}/handler.py" "${dest}/.install_receipt.json"
    # Chown so the Hermes runtime uid can read them. Skip silently if the
    # caller isn't root and can't chown — the copy already worked.
    chown "${HERMES_UID}:${HERMES_GID}" "${dest}/HOOK.yaml" "${dest}/handler.py" "${dest}/.install_receipt.json" 2>/dev/null || true
    echo "install: ${hook} -> ${dest} (receipt written)"
done

cat <<EOF
install: done — both hooks + receipts in place.

install: NEXT STEP (not done for you — the gateway only discovers hooks at startup):
install:   ${HERMES_BIN} gateway restart
install: then confirm both hooks loaded — look for "2 hook(s) loaded" or the hook names:
install:   grep -E 'hook\(s\) loaded|u1_grace_cancel|u1_confirm_start' ${GATEWAY_LOG} | tail -5
install: or just run:
install:   bash ${HERE}/install_hermes_u1_hooks.sh --verify

install: until that restart, a YES at the bed-clear prompt redeems nothing
install: (fail-safe: the printer never starts) and reply-CANCEL is dead during
install: grace windows.
EOF
