# Verifying the Telegram cancel chain (grace-period hook)

The reply-CANCEL path is deliberately **model-free**: the Hermes gateway
process runs the hook (regex match → touch marker file) on the raw incoming
message *before* the LLM agent ever sees it, and the print-start gate polls
the marker on the filesystem. Whatever the agent says or does in chat after
you type CANCEL is a parallel side-show — this page verifies the part that
matters.

The honest dependency list: Telegram's infrastructure → gateway process
alive → **hook actually installed** → filesystem. The third is the only one
that's ever silently absent, and it's what these checks confirm.

Run everything on the Hermes host (or `docker exec -it hermes-agent-stack
bash` first).

---

## 1. Confirm the hook is installed

```bash
HOOKS_DIR=$(/opt/hermes/.venv/bin/python -c 'from gateway.hooks import HOOKS_DIR; print(HOOKS_DIR)')
ls -la "$HOOKS_DIR/u1_grace_cancel/"        # should show HOOK.yaml + handler.py
ls -la /opt/data/.u1_cancel_hook_receipt    # receipt = installer verified it loaded
```

No directory or no receipt → run `bash tools/install_hermes_cancel_hook.sh`
and re-check. Without the receipt, the grace-period DM deliberately
advertises the SSH fallback instead of promising reply-CANCEL.

## 2. Read the hook's own log

Ground truth for whether a CANCEL reply reached the hook:

```bash
cat "$HOOKS_DIR/u1_grace_cancel/hook.log"
```

| Entry | Meaning |
|---|---|
| `cancel_marker_touched` | Full chain works. Done. |
| `cancel_ignored_no_pending_window` | Hook heard you, but no grace window was open (replied after expiry, or the notify never wrote the pending file — check `HERMES_BIN`, see README). |
| `cancel_code_no_match` | Scoped `cancel <code>` with a code matching no active window — cancelled nothing, by design. |
| file missing / empty | Hook never fired: not installed, or the `agent:start` event isn't delivering message text in this Hermes build. Investigate before trusting reply-CANCEL. |

## 3. Check the gate's side of the story

For the request you tested:

```bash
ls -t /opt/data/snapmaker_u1/requests | head -3     # newest request id
python3 /opt/data/scripts/u1_audit.py show <request_id> | grep -i grace
```

Expect `pre_start_grace_period_started`, then exactly one of:

- `pre_start_grace_cancelled` — you cancelled; no HTTP reached the printer.
  The refusal payload carries `recovery.stage1_command` (fresh photo + fresh
  yes; no re-slice needed).
- `pre_start_grace_period_expired` — window ran out; print proceeded.

Also meaningful: `pre_start_grace_notify_failed` means the DM was never
sent (typically `hermes` not on the subprocess PATH — set `HERMES_BIN`,
see `.env.example`). The wait still runs fail-open in that case.

## 4. Zero-risk live drill (no printer involved)

Seed a fake grace window by hand, then reply in Telegram:

```bash
mkdir -p /tmp/u1_pending_cancel
cat > /tmp/u1_pending_cancel/u1_2026_0702_dr1ll0.json <<'EOF'
{
  "request_id": "u1_2026_0702_dr1ll0",
  "cancel_marker": "/tmp/u1_cancel_drill.marker",
  "filename": "drill.gcode",
  "grace_seconds": 300,
  "expires_at": ""
}
EOF
```

Type `CANCEL` in your Telegram DM, wait a couple of seconds, then:

```bash
ls -la /tmp/u1_cancel_drill.marker && cat /tmp/u1_cancel_drill.marker
```

Marker exists → the model-free path is proven end to end. Repeat the drill
for the other match modes:

- `cancel dr1ll0` — scoped to this window's code (last 6 chars of the
  request id): marker appears.
- `cancel wrong99` — matches nothing: marker must NOT appear
  (`cancel_code_no_match` in hook.log).
- `cancel that plan for tomorrow` — prose: ignored entirely.

Clean up:

```bash
rm -f /tmp/u1_cancel_drill.marker /tmp/u1_pending_cancel/u1_2026_0702_dr1ll0.json
```

## 5. Full live checklist (real print)

Once the drill passes, the remaining hardware verification for v2.1.0:

1. Bare `CANCEL` during a real grace window → refused, audit row, recovery
   command offered.
2. `cancel <code>` with the DM's code → cancels; wrong code → nothing.
3. Reply in the final ~2 seconds of the window → still caught.
4. One run with the receipt removed → DM advertises SSH fallback, not
   reply-CANCEL.
5. After a cancel: run the `recovery.stage1_command` → fresh photo + fresh
   yes → print starts without re-slicing.
6. Kit flow: confirm the yes-command carries `--pending-nonce`, and a
   hand-typed confirm without it is refused.
7. `--operator smoke:test` end-to-end → TEST MODE banner, Stage 2 refused.
