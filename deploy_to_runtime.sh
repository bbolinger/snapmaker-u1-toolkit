#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
# Resolve default destination roots based on where the script is invoked from.
# Inside the Hermes container (`hermes-agent-stack`) the bind mount lives at
# /opt/data. From any other container/host (e.g. dev-container) the same
# bind appears at /appdata/hermes. /opt/data on those hosts is the LOCAL
# container filesystem and writes there are invisible to Hermes — a silent
# failure mode that landed several v1.5.x deploys in a dead-end during the
# 2026-06-25 live test. Detect by checking whether /appdata/hermes/scripts/
# exists (the marker of a Hermes-shared dataset).
if [[ -d /appdata/hermes/scripts ]] && [[ "$ROOT" == /appdata/hermes/* ]]; then
  _DEFAULT_ROOT="/appdata/hermes"
elif [[ -d /opt/data ]]; then
  _DEFAULT_ROOT="/opt/data"
elif [[ -n "${HERMES_HOME:-}" ]] && [[ -d "${HERMES_HOME}" ]]; then
  # Native Windows (Git Bash) / non-container installs: no /opt/data.
  # Deploy into the Hermes home the gateway actually reads from.
  _DEFAULT_ROOT="${HERMES_HOME}"
else
  echo "deploy: no deploy root found." >&2
  echo "  Neither /opt/data nor \$HERMES_HOME exists on this host." >&2
  echo "  Native Windows: run from Git Bash with HERMES_HOME set to your" >&2
  echo "  Hermes data dir, or set U1_DEPLOY_SCRIPTS / U1_DEPLOY_TOOLS /" >&2
  echo "  U1_DEPLOY_SKILL / U1_DEPLOY_PROFILES explicitly." >&2
  exit 1
fi
SCRIPT_DST="${U1_DEPLOY_SCRIPTS:-$_DEFAULT_ROOT/scripts}"
TOOLS_DST="${U1_DEPLOY_TOOLS:-$_DEFAULT_ROOT/tools}"
SKILL_DST="${U1_DEPLOY_SKILL:-$_DEFAULT_ROOT/skills/hardware-automation/3d-printer-slicing-automation}"
PROFILES_DST="${U1_DEPLOY_PROFILES:-$_DEFAULT_ROOT/profiles}"
PROFILES_SRC="$ROOT/profiles"
SKILL_SRC="$ROOT/skills/3d-printer-slicing-automation"
BACKUPS=()
CHANGED=()

backup_one() {
  local dst="$1"
  if [[ -e "$dst" ]]; then
    local bak="${dst}.bak-mirror-${STAMP}"
    if [[ ! -e "$bak" ]]; then
      cp -a "$dst" "$bak"
      BACKUPS+=("$bak")
    fi
  fi
}

copy_if_changed() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [[ -e "$dst" ]] && cmp -s "$src" "$dst"; then
    return 0
  fi
  backup_one "$dst"
  cp -a "$src" "$dst"
  CHANGED+=("$dst")
}

# Scripts: only u1_*.py from the workspace.
for src in "$ROOT"/scripts/u1_*.py; do
  [[ -e "$src" ]] || continue
  copy_if_changed "$src" "$SCRIPT_DST/$(basename "$src")"
done

# Tools: all workspace tools/*.py.
for src in "$ROOT"/tools/*.py; do
  [[ -e "$src" ]] || continue
  copy_if_changed "$src" "$TOOLS_DST/$(basename "$src")"
done

# Profiles dir: workflow's picker scans `from-printer/`, `user/`, and
# `snapmaker-stock/` which are PER-USER (gitignored) and populated by
# the fetch / extract scripts at the runtime location. The deploy must
# NOT touch those subdirs — wiping them on every redeploy would force
# the operator to re-fetch ~217 stock files every time. Sync only the
# repo-tracked bits: `machine/` + the top-level README.
if [[ -d "$PROFILES_SRC" ]]; then
  mkdir -p "$PROFILES_DST"
  if [[ -d "$PROFILES_SRC/machine" ]]; then
    if [[ -d "$PROFILES_DST/machine" ]]; then
      if ! diff -qr "$PROFILES_SRC/machine" "$PROFILES_DST/machine" >/dev/null 2>&1; then
        bak="${PROFILES_DST}/machine.bak-mirror-${STAMP}"
        cp -a "$PROFILES_DST/machine" "$bak"
        BACKUPS+=("$bak")
        rm -rf "$PROFILES_DST/machine"
        cp -a "$PROFILES_SRC/machine" "$PROFILES_DST/machine"
        CHANGED+=("$PROFILES_DST/machine")
      fi
    else
      cp -a "$PROFILES_SRC/machine" "$PROFILES_DST/machine"
      CHANGED+=("$PROFILES_DST/machine")
    fi
  fi
  if [[ -f "$PROFILES_SRC/README.md" ]]; then
    copy_if_changed "$PROFILES_SRC/README.md" "$PROFILES_DST/README.md"
  fi
fi

# Skill dir: snapshot existing dir once, then rsync/copy only if source differs.
# Backups go to a SIBLING `skills_backups/` directory rather than at the same
# level as the live skill — Hermes scans the skill category dir for installed
# skills, and leaving `*.bak-mirror-*` siblings of the live skill confuses
# agents into asking "which version do I load?" (caught live during the first
# v1.5.0 slice attempt).
SKILL_CATEGORY="$(dirname "$SKILL_DST")"
SKILL_BAK_ROOT="$(dirname "$SKILL_CATEGORY")_backups/$(basename "$SKILL_CATEGORY")"

# v1.4.x → v1.5.0 migration: the old deploy script left backups as
# `${SKILL_DST}.bak-mirror-*` siblings of the live skill — inside the
# category dir Hermes scans for installed skills. That confused agents
# into asking "which version do I load?". Relocate any pre-v1.5.0
# backups to the new sibling skills_backups/ root before continuing.
# Idempotent: nothing to migrate after the first run.
MIGRATED_BAKS=()
shopt -s nullglob
for old_bak in "$SKILL_DST".bak-mirror-*; do
  [[ -e "$old_bak" ]] || continue
  mkdir -p "$SKILL_BAK_ROOT"
  mv "$old_bak" "$SKILL_BAK_ROOT/"
  MIGRATED_BAKS+=("$old_bak → $SKILL_BAK_ROOT/$(basename "$old_bak")")
done
shopt -u nullglob

if [[ -d "$SKILL_SRC" ]]; then
  if [[ -d "$SKILL_DST" ]]; then
    if ! diff -qr "$SKILL_SRC" "$SKILL_DST" >/dev/null 2>&1; then
      mkdir -p "$SKILL_BAK_ROOT"
      bak="$SKILL_BAK_ROOT/$(basename "$SKILL_DST").bak-mirror-${STAMP}"
      cp -a "$SKILL_DST" "$bak"
      BACKUPS+=("$bak")
      rm -rf "$SKILL_DST"
      mkdir -p "$(dirname "$SKILL_DST")"
      cp -a "$SKILL_SRC" "$SKILL_DST"
      CHANGED+=("$SKILL_DST")
    fi
  else
    mkdir -p "$(dirname "$SKILL_DST")"
    cp -a "$SKILL_SRC" "$SKILL_DST"
    CHANGED+=("$SKILL_DST")
  fi
  chown -R 10000:10000 "$SKILL_DST" 2>/dev/null || true
fi

# v1.6 (2026-06-26): deploy HERMES.md to Hermes' prompt-assembly stable
# tier. Hermes' _find_hermes_md (agent/prompt_builder.py) walks from
# Hermes daemon CWD (/opt/hermes inside the container) UPward looking for
# .hermes.md or HERMES.md, stopping at git root or filesystem root.
#
# Path mapping makes this tricky: /opt/data IS bind-mounted from
# /appdata/hermes but ISN'T on Hermes' walk path. /opt/ IS on the walk
# path but is container-private — we have to write into it via docker exec.
#
# Strategy: keep the canonical copy at /opt/data/.hermes.md (so it
# persists across container recreates via the bind mount), then docker
# exec to also place it at /opt/.hermes.md (container-private but on the
# walk path). The container-private one is re-created on every deploy.
HERMES_MD_SRC="$ROOT/HERMES.md"
if [[ -f "$HERMES_MD_SRC" ]]; then
  # Canonical persistent copy
  HERMES_MD_PERSISTENT="$_DEFAULT_ROOT/.hermes.md"
  copy_if_changed "$HERMES_MD_SRC" "$HERMES_MD_PERSISTENT"
  chown 10000:10000 "$HERMES_MD_PERSISTENT" 2>/dev/null || true
  # Container-private walk-path copy (Hermes finds this at
  # /opt/.hermes.md when its prompt_builder walks up from /opt/hermes)
  if command -v docker >/dev/null 2>&1 && docker inspect hermes-agent-stack >/dev/null 2>&1; then
    docker exec hermes-agent-stack cp /opt/data/.hermes.md /opt/.hermes.md 2>/dev/null || true
    docker exec hermes-agent-stack chown 10000:10000 /opt/.hermes.md 2>/dev/null || true
    CHANGED+=("/opt/.hermes.md (via docker exec)")
  fi
fi

# Host writes under /appdata/hermes must be readable by Hermes uid 10000.
for path in "${CHANGED[@]}"; do
  case "$path" in
    /appdata/hermes/*) chown -R 10000:10000 "$path" 2>/dev/null || true ;;
  esac
done

printf '\nDeploy summary\n'
printf 'Changed files/dirs:\n'
if ((${#CHANGED[@]})); then printf '  %s\n' "${CHANGED[@]}"; else printf '  none (no-op)\n'; fi
printf 'Backups created:\n'
if ((${#BACKUPS[@]})); then printf '  %s\n' "${BACKUPS[@]}"; else printf '  none\n'; fi
if ((${#MIGRATED_BAKS[@]})); then
  printf 'Migrated pre-v1.5.0 skill backups (relocated out of the live category dir):\n'
  printf '  %s\n' "${MIGRATED_BAKS[@]}"
fi

printf '\nDiff summary (workspace vs runtime after deploy):\n'
for src in "$ROOT"/scripts/u1_*.py; do
  [[ -e "$src" ]] || continue
  dst="$SCRIPT_DST/$(basename "$src")"
  if [[ -e "$dst" ]]; then diff -q "$src" "$dst" || true; fi
done
for src in "$ROOT"/tools/*.py; do
  [[ -e "$src" ]] || continue
  dst="$TOOLS_DST/$(basename "$src")"
  if [[ -e "$dst" ]]; then diff -q "$src" "$dst" || true; fi
done
if [[ -d "$SKILL_DST" ]]; then diff -qr "$SKILL_SRC" "$SKILL_DST" || true; fi

# ===========================================================================
# Post-deploy env validation
# ===========================================================================
# After files are in place, verify the deployed workflow can actually start.
# We invoke `u1_slice_workflow.py --help` rather than reimplementing the
# candidate-list logic in bash. This is the most accurate test:
#   - Goes through the workflow's own _ensure_compat_python() bootstrap
#   - Resolves candidates relative to the DEPLOYED __file__ location, not
#     the workspace location (matters when U1_DEPLOY_* env vars point
#     somewhere other than the workspace root)
#   - Honors U1_TOOLKIT_PYTHON the same way the runtime invocation will
#   - Single source of truth: the workflow's bootstrap, no drift risk

printf '\nEnvironment validation\n'

DEPLOYED_WORKFLOW="$SCRIPT_DST/u1_slice_workflow.py"
if [[ ! -f "$DEPLOYED_WORKFLOW" ]]; then
  printf '  ⚠  workflow not at %s — env check skipped\n' "$DEPLOYED_WORKFLOW"
else
  HELP_RC=0
  # Capture stderr+stdout. The workflow's bootstrap prints "[env] current
  # python lacks X; switching to Y" to stderr if it re-execs; that's
  # information worth surfacing on success.
  HELP_OUTPUT=$("$DEPLOYED_WORKFLOW" --help 2>&1) || HELP_RC=$?

  if (( HELP_RC == 0 )); then
    printf '  ✓  workflow starts cleanly at %s\n' "$DEPLOYED_WORKFLOW"
    # If the bootstrap re-execed, surface which interpreter it landed on
    # so users can verify their U1_TOOLKIT_PYTHON took effect.
    if echo "$HELP_OUTPUT" | grep -q '^\[env\] current python lacks'; then
      RESOLVED=$(echo "$HELP_OUTPUT" | grep '^\[env\] current python lacks' | sed 's/.*switching to //')
      printf '  ℹ  Auto-detected interpreter: %s\n' "$RESOLVED"
    fi
    printf '  ✓  Deploy complete.\n'

    # v1.5.0: the picker scans profiles/{from-printer,user,snapmaker-stock}/
    # in the deployed location too. If the runtime profiles dir is empty,
    # the workflow exits with a setup_required event on first run — surface
    # the fix path now so operators don't have to learn it from a failed
    # invocation.
    DEPLOYED_TOOLS="${U1_DEPLOY_TOOLS:-/opt/data/tools}"
    PROFILES_AT_RUNTIME="$(dirname "$SCRIPT_DST")/profiles"
    if [[ -d "$PROFILES_AT_RUNTIME" ]]; then
      # Loop over the picker source subdirs to avoid the `set -e` +
      # missing-dir trap (find on a non-existent path exits non-zero,
      # which used to abort the deploy script silently right after the
      # ✓ Deploy complete line).
      profile_count=0
      for _d in from-printer user snapmaker-stock; do
        if [[ -d "$PROFILES_AT_RUNTIME/$_d" ]]; then
          profile_count=$(( profile_count + $(find "$PROFILES_AT_RUNTIME/$_d" -name '*.json' -type f 2>/dev/null | wc -l) ))
        fi
      done
      if (( profile_count == 0 )); then
        printf '\n  ℹ  Profile picker is empty at %s/{from-printer,user,snapmaker-stock}/\n' "$PROFILES_AT_RUNTIME"
        printf '     Populate before the first slice:\n'
        printf '       python3 %s/fetch_snapmaker_profiles.py    # Snapmaker U1 stock baseline (~217 files)\n' "$DEPLOYED_TOOLS"
        printf '       python3 %s/extract_profiles_from_printer.py    # extract from your printer'\''s recent history\n' "$DEPLOYED_TOOLS"
        printf '     (See TROUBLESHOOTING.md for details on the v1.5.0 empty-picker setup.)\n'
      else
        printf '  ✓  %d profile JSON(s) found across runtime sources.\n' "$profile_count"
      fi
    fi
  else
    printf '\n  ✗  Deployed workflow failed to start (exit %d).\n' "$HELP_RC"
    printf '  Captured output from %s --help:\n' "$DEPLOYED_WORKFLOW"
    echo "$HELP_OUTPUT" | sed 's/^/    /'
    printf '\n  Files were copied OK — only the env/startup check failed.\n'
    printf '  The workflow output above tells you which Python interpreters were\n'
    printf '  tried and how to fix. Re-run this script after fixing to confirm.\n\n'
    exit 3
  fi
fi

# ===========================================================================
# Gateway hook receipts check (v2.3)
# ===========================================================================
# The model-free start chain needs TWO gateway hooks installed where the
# Hermes gateway actually runs: u1_confirm_start (the operator YES that
# starts the print) and u1_grace_cancel (reply-CANCEL during the grace
# window). This script deliberately does NOT install them — deploys can run
# on boxes where the gateway lives elsewhere. It only checks for the
# receipts tools/install_hermes_u1_hooks.sh writes, and yells when they're
# absent: a fresh install without the confirm hook arms YES windows that
# nothing redeems (fail-safe, but the printer never starts).
_HOOKS_DIR="${HERMES_HOOKS_DIR:-}"
if [[ -z "$_HOOKS_DIR" ]]; then
  _HOOKS_DIR="$("${HERMES_PY:-/opt/hermes/.venv/bin/python}" -c 'from gateway.hooks import HOOKS_DIR; print(HOOKS_DIR)' 2>/dev/null || true)"
fi
if [[ -z "$_HOOKS_DIR" ]]; then
  # No Hermes python here (e.g. deploying from the host side of the bind
  # mount) — fall back to the same root the deploy targets. Inside the
  # Hermes container that's /opt/data/hooks; from the host it's
  # /appdata/hermes/hooks (the same dir through the bind).
  _HOOKS_DIR="$_DEFAULT_ROOT/hooks"
fi
_CANCEL_RECEIPT=""
_CONFIRM_RECEIPT=""
if [[ -s "$_HOOKS_DIR/u1_grace_cancel/.install_receipt.json" ]]; then _CANCEL_RECEIPT=1; fi
if [[ -s "$_HOOKS_DIR/u1_confirm_start/.install_receipt.json" ]]; then _CONFIRM_RECEIPT=1; fi
if [[ -z "$_CANCEL_RECEIPT" && -z "$_CONFIRM_RECEIPT" ]]; then
  printf '\n'
  printf '############################################################################\n'
  printf '##                                                                        ##\n'
  printf '##   WARNING: MODEL-FREE START HOOK NOT INSTALLED                         ##\n'
  printf '##                                                                        ##\n'
  printf '############################################################################\n'
  printf '\n'
  printf '  No hook install receipts found in: %s\n' "$_HOOKS_DIR"
  printf '\n'
  printf '  Without the u1_confirm_start gateway hook, the workflow still arms the\n'
  printf '  YES window at the bed-clear prompt but NOTHING redeems it: the operator\n'
  printf '  replies YES and the printer never starts (fail-safe, but dead). Without\n'
  printf '  u1_grace_cancel, reply-CANCEL during the grace window is dead too.\n'
  printf '\n'
  printf '  On the box where the Hermes gateway runs:\n'
  printf '      bash tools/install_hermes_u1_hooks.sh\n'
  printf '  then restart the gateway and confirm:\n'
  printf '      bash tools/install_hermes_u1_hooks.sh --verify\n'
  printf '\n'
  printf '############################################################################\n'
elif [[ -z "$_CONFIRM_RECEIPT" ]]; then
  printf '\n  ⚠  u1_confirm_start hook receipt missing in %s — the operator YES at the\n' "$_HOOKS_DIR"
  printf '     bed-clear prompt redeems nothing until it is installed (the printer\n'
  printf '     never starts). Run: bash tools/install_hermes_u1_hooks.sh\n'
elif [[ -z "$_CANCEL_RECEIPT" ]]; then
  printf '\n  ⚠  u1_grace_cancel hook receipt missing in %s — reply-CANCEL during the\n' "$_HOOKS_DIR"
  printf '     grace window does nothing until it is installed.\n'
  printf '     Run: bash tools/install_hermes_u1_hooks.sh\n'
else
  # Receipts exist — but do they match THIS workspace's hook sources? A
  # STALE installed hook is worse than a missing one: v2.4.1 moved the
  # pending-marker dirs, and an old loaded cancel hook reading the old
  # location leaves the grace DM advertising a reply-CANCEL nothing hears
  # (the legacy-dir shim in the handlers covers one release, but yell
  # anyway so the skew gets fixed, not relied on).
  _sha256_local() {
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "$1" | awk '{print $1}'
    else
      shasum -a 256 "$1" | awk '{print $1}'
    fi
  }
  _STALE_HOOKS=""
  for _hook in u1_grace_cancel u1_confirm_start; do
    _want="$(_sha256_local "$ROOT/tools/hermes_hooks/$_hook/handler.py" 2>/dev/null || true)"
    _have="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['sha256'].get('handler.py',''))" \
             "$_HOOKS_DIR/$_hook/.install_receipt.json" 2>/dev/null || true)"
    if [[ -n "$_want" && -n "$_have" && "$_want" != "$_have" ]]; then
      _STALE_HOOKS="$_STALE_HOOKS $_hook"
    fi
  done
  if [[ -n "$_STALE_HOOKS" ]]; then
    printf '\n  ⚠  STALE gateway hook(s) installed:%s\n' "$_STALE_HOOKS"
    printf '     The installed handler.py does not match this workspace. Re-run:\n'
    printf '         bash tools/install_hermes_u1_hooks.sh\n'
    printf '     then restart the gateway — a stale cancel hook can miss the\n'
    printf '     pending-marker location this deploy just shipped.\n'
  else
    printf '\n  ✓  Both gateway hook receipts present and current in %s.\n' "$_HOOKS_DIR"
  fi
fi
