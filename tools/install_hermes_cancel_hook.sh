#!/bin/bash
# install_hermes_cancel_hook.sh — superseded by install_hermes_u1_hooks.sh,
# which installs BOTH U1 gateway hooks (u1_grace_cancel + u1_confirm_start)
# and writes per-hook install receipts. This pointer keeps old runbooks and
# muscle memory working. Note one behavior change: the unified installer
# does NOT restart the gateway — it prints the restart + verify steps.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "install_hermes_cancel_hook.sh is superseded by tools/install_hermes_u1_hooks.sh (installs both U1 hooks + receipts) — running that instead."
exec bash "${HERE}/install_hermes_u1_hooks.sh" "$@"
