#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
# Paths use the Hermes-container view by default (where the host's
# /appdata/hermes is bind-mounted at /opt/data). Override via env vars
# for other deployments.
SCRIPT_DST="${U1_DEPLOY_SCRIPTS:-/opt/data/scripts}"
TOOLS_DST="${U1_DEPLOY_TOOLS:-/opt/data/tools}"
SKILL_DST="${U1_DEPLOY_SKILL:-/opt/data/skills/hardware-automation/3d-printer-slicing-automation}"
PROFILES_DST="${U1_DEPLOY_PROFILES:-/opt/data/profiles}"
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

# Profiles dir: workflow's profile picker resolves __file__.parent.parent/profiles
# so this must land beside the deployed scripts.
if [[ -d "$PROFILES_SRC" ]]; then
  if [[ -d "$PROFILES_DST" ]]; then
    if ! diff -qr "$PROFILES_SRC" "$PROFILES_DST" >/dev/null 2>&1; then
      bak="${PROFILES_DST}.bak-mirror-${STAMP}"
      cp -a "$PROFILES_DST" "$bak"
      BACKUPS+=("$bak")
      rm -rf "$PROFILES_DST"
      mkdir -p "$(dirname "$PROFILES_DST")"
      cp -a "$PROFILES_SRC" "$PROFILES_DST"
      CHANGED+=("$PROFILES_DST")
    fi
  else
    mkdir -p "$(dirname "$PROFILES_DST")"
    cp -a "$PROFILES_SRC" "$PROFILES_DST"
    CHANGED+=("$PROFILES_DST")
  fi
fi

# Skill dir: snapshot existing dir once, then rsync/copy only if source differs.
if [[ -d "$SKILL_SRC" ]]; then
  if [[ -d "$SKILL_DST" ]]; then
    if ! diff -qr "$SKILL_SRC" "$SKILL_DST" >/dev/null 2>&1; then
      bak="${SKILL_DST}.bak-mirror-${STAMP}"
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
