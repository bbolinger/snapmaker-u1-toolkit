# Snapmaker U1 safety gates — Stage 1 / Stage 2 detail

The skill's Step 4 names the two gates. This reference holds the field-level
detail Gemma needs only when actually running them.

## Invariants (always true)

- Default upload path is `print=false` / upload-only.
- Starting requires idle printer state, expected tool/material, successful
  upload, fresh LED-on bed photo, AND explicit operator `Start print` selection.
- Bed-clear prompt defaults to Cancel.
- Unknown or stale evidence means stop.

## Stage 1 — readiness + photo + token

Run the `start_gate_stage1_command` from the `readiness_card` event. This call
NEVER starts the print. It returns:

| Field | Meaning |
|---|---|
| `blockers` | Array of preflight problems. Empty = ready. |
| `snapshot` | `{path, is_mock, fresh, brightness_mean, brightness_ok, brightness_check, brightness_check_reason, sha256, error}` |
| `approval_token` | Short hex string. REQUIRED for Stage 2. `null` if Stage 1 failed unrecoverably. |
| `approval_ttl_seconds` | How long the token is valid (currently 1800s = 30 min). |
| `next_step` | The exact Stage 2 command with the token baked in. |

### Send the photo FIRST

Always — before any verdict, before any condition you read off the event. The
operator is the gatekeeper, and they need to see the image. Write
`snapshot.path` (the absolute path) BARE in your reply text — Hermes
auto-attaches absolute paths. Do NOT wrap it in backticks or a code fence
(gateway skips those). One sentence like `Bed photo: /opt/data/snapmaker_u1/bed_snapshot.jpg`
and the image appears in the chat.

### Refuse only on real failures

Two conditions block a Stage 1 → Stage 2 progression:

- `snapshot.is_mock: true` — camera unreachable; the file is a labeled mock, not bed evidence.
- `snapshot.brightness_check: "measured"` AND `snapshot.ok: false` — we measured a verifiably dark frame.

Everything else proceeds, including `snapshot.brightness_check: "deferred"`
(PIL/Pillow wasn't available; the photo IS real — operator judges). Surface
deferred-reason as context but keep going.

Surface every entry in `blockers` verbatim — those ARE blockers (paused,
busy, wrong tool, wrong material loaded). Empty blockers + usable photo =
Stage 2 reachable.

### The approval question

> "Review the attached photo. Bed clear and you want to start request `<request_id>`? (yes/no)"

Substitute `<request_id>` from the `readiness_card` event. Default = no. Do
NOT decide bed clearance for the operator. Their reply IS the gate. See
[`approval-phrasing.md`](approval-phrasing.md) for templates + rationale.

## Stage 2 — actual start (only after explicit yes + valid token)

If user said yes AND blockers empty AND snapshot usable, run Stage 2. Use the
`start_gate_stage1_command` the workflow emitted as your base (already
includes `--request-id` and `--operator`); add `--bed-clear start` and
`--approval-token <token>`:

```bash
python3 /opt/data/scripts/u1_print_start_gate.py <printer_storage_filename> \
  --intended-tool extruder<N> --requested-material <material> \
  --request-id u1_YYYY_MMDD_xxxxxx --operator <operator-id> \
  --bed-clear start --approval-token <token-from-stage-1>
```

The gate validates the token (30-min TTL), re-runs preflight, takes a
sanity-only fresh capture (NOT shown — operator already approved Stage 1's
photo), AND routes through `can_start()` (v2.0 Phase 3b) to verify the print
plan hasn't drifted since review. Stage 2 starts only if all four checks
pass. If anything refuses, surface the gate's `reason` field verbatim. See
[`can-start-refusal-handling.md`](can-start-refusal-handling.md) for the
reject branches + recovery procedure.

### Expired-token recovery

If Stage 2 refuses with `approval token invalid` / token age / TTL expired,
the prior operator "yes" is spent and does NOT authorize a new start. Re-run
the workflow for the same STL with `--request-id <request_id>` to recover the
readiness card, then tool-call the emitted Stage-1 command verbatim. Surface
the fresh bed photo and ask a new approval question with the same
`request_id`. Only after a fresh "yes" may you run Stage 2 with the new token.

### Pre-start grace period (v2.1.0)

After every safety check passes AND before `u1_print_start_gate.py` HTTPs
the printer's `/printer/print/start`, the gate enters a **grace window**
(default 120s, configurable via `U1_GRACE_PERIOD_SECONDS` env var or
`--grace-seconds N`, `0` disables). The gate audit writes
`pre_start_grace_period_started` to `<request_dir>/audit.jsonl` with
`{grace_seconds, cancel_marker}` fields.

**Adapter role:** watch the audit log for `pre_start_grace_period_started`
and, on match, send the operator a loud notification (Telegram, Discord,
whatever the channel is) with a cancel action. The cancel action must
create the file at `cancel_marker` (any content). The gate polls once per
second; when the file appears, the gate refuses with:
`Operator cancelled during the pre-start grace period. No HTTP call was
sent to the printer.` No print, no material burned, `print_started` audit
row is NEVER written.

If the grace window expires with no cancel marker, the gate audits
`pre_start_grace_period_expired` and proceeds to HTTP the printer.

This is the mechanical safety net: if the skill (or any agent) chain-fires
the yes-command → Stage 2 command, the operator still gets a real-world
notification with time to react before the printer starts moving material.

### After Stage 2 succeeds

Report only the start result fields (`started`, `response`, `blockers`,
filename/tool/material). Do **not** surface or attach the Stage-2 sanity
snapshot path in the final message; that capture is an internal safety check,
not a second operator-review artifact. The only bed photo the operator
should review is Stage 1's approval photo.

## Hard rules (no exceptions)

- DO NOT skip Stage 1.
- DO NOT invent a magic phrase.
- DO NOT pass `--bed-clear start` without the token AND the operator's
  explicit yes. Default = cancel.
