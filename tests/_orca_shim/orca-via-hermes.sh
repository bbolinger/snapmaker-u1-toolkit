#!/bin/sh
# Orca-via-Hermes shim — runs OrcaSlicer via `docker exec hermes-agent-stack`.
#
# Why: dev-container is Alpine (musl), so it can't directly execute the
# glibc Orca appimage. Hermes is Debian and has Orca's bundled libs at
# /opt/data/tools/orcaslicer/local-libs/usr/lib/x86_64-linux-gnu — the same
# env production uses.
#
# Path translation: dev-container's /appdata/hermes/* is bind-mounted
# at /opt/data/* inside Hermes. We translate every /appdata/hermes/...
# substring within each arg — not just the prefix — so semicolon-separated
# multi-path args like `--load-settings '/appdata/hermes/A.json;/appdata/hermes/B.json'`
# get both halves translated.
#
# LD_LIBRARY_PATH: docker exec doesn't forward env by default. We hardcode
# the production LD_LIBRARY_PATH inside Hermes so Orca finds the bundled
# libOpenGL.so.0.

ORCA_HERMES_PATH=/opt/data/tools/orcaslicer/squashfs-root/AppRun
HERMES_CONTAINER=hermes-agent-stack
HERMES_LIBS=/opt/data/tools/orcaslicer/local-libs/usr/lib/x86_64-linux-gnu:/opt/data/tools/orcaslicer/squashfs-root/usr/lib

# Snapshot original args (can't use `set --` mid-iteration without losing them)
i=0
for arg in "$@"; do
  i=$((i + 1))
  eval "arg_$i=\"\$arg\""
done
n=$i

# Translate each arg, then re-stack
set --
i=0
while [ $i -lt $n ]; do
  i=$((i + 1))
  eval "v=\$arg_$i"
  # Per-substring substitution: handles single args containing multiple
  # /appdata/hermes/... paths (e.g. --load-settings 'A;B').
  v=$(printf '%s' "$v" | sed 's|/appdata/hermes/|/opt/data/|g')
  set -- "$@" "$v"
done

exec docker exec -e "LD_LIBRARY_PATH=$HERMES_LIBS" "$HERMES_CONTAINER" "$ORCA_HERMES_PATH" "$@"
