# Safety model — concrete details

The high-level model is in the [README](../README.md#safety-model). This is the per-action breakdown, the test-operator fence, the pre-start grace period with the model-free Telegram cancel, and the model-free YES that starts the print.

---


```
read → slice → upload (print=false) → operator-approved start → quiet monitor
```

**Allowed automatically**:
- Read printer state through Moonraker/Klipper endpoints
- Read toolhead/extruder/material/feed sensor state
- Read G-code metadata
- Upload/stage G-code with `print=false`
- Trigger fresh camera snapshots
- Send operator alerts for milestones or issues
- Record local print history

**Requires explicit operator approval**:
- Start a print
- Resume a paused print
- Cancel/stop a print
- Any movement/heating command

### Test-operator fence
`u1_print_start_gate.py` refuses Stage 2 (real printer start) BEFORE any Moonraker
call if the `--operator` argument starts with an unambiguously test-flavored
prefix: `smoke:`, `test:`, `dry:`, `mock:`, or `fixture:` (case-insensitive).

The gate returns a `gate_refused_test_operator` payload and audits the refusal.
No HTTP call reaches the printer, no photo is taken, no preflight runs. This
closes the "smoke-test accidentally runs a real print" failure that motivated
the fence.

`u1_kit_workflow.py` also prints a highly visible TEST MODE banner to stderr
at the top of every invocation under a test-flavored operator, so the tester
sees the gate will refuse before any command runs.

If you hit the fence for a legitimate print — e.g. you set `U1_OPERATOR=test:homelab`
in your environment because that's your naming convention — change the operator
string to something without a test prefix (`homelab`, `human:homelab`, etc.).
The fence is deliberately narrow: identity strings starting with `dev:`, `ci:`,
`telegram:`, `discord:`, `human:`, or bare names all proceed normally.

Two sharp edges, decided deliberately (rc2):

- An explicit `--operator` now rides **every** command the kit workflow
  emits, so a `smoke:*` identity can't silently drop back to your
  production `U1_OPERATOR` mid-chain. Env-resolved identity is never baked
  into commands (replay-safe).
- An **unset** operator (`unknown:gate`) passes the fence — refusing every
  bare CLI run would tax legitimate local use — but leaves a loud
  `gate_operator_unknown` audit row. If your smoke tests might run with no
  operator set, set one (`smoke:whatever`) so the fence can catch them.

### Pre-start grace period + Telegram cancel button
After every safety check passes and before the gate HTTPs the printer's
`/printer/print/start`, there's a **grace window** (default 120s, configurable
via `U1_GRACE_PERIOD_SECONDS` env var or `--grace-seconds N`). During the
window the gate:

1. Fires an operator notification via `$U1_GRACE_NOTIFY_CMD` (a shell
   command you configure). Env vars `U1_REQUEST_ID`, `U1_FILENAME`,
   `U1_GRACE_SECONDS`, `U1_CANCEL_MARKER`, `U1_OPERATOR` are exported so
   your command can templatize them.
2. Polls a per-request marker file at `<out_dir>/pre_start_cancel.marker`
   once per second.
3. If the marker appears → refuses the print, no HTTP call, audit
   `pre_start_grace_cancelled`.
4. If it doesn't appear before the window expires → proceeds, audit
   `pre_start_grace_period_expired`.

**Hermes users get a one-tap cancel button:** ship a Telegram notification
via `hermes send` and a Gateway hook that touches the marker when you reply
`cancel <code>` in the DM. Zero LLM, zero agent-loop — the hook runs directly
in the Hermes gateway process.

Install (one installer covers this hook AND the confirm-start hook below):

```bash
# One-time, on the box where the Hermes gateway runs — installs BOTH U1
# gateway hooks (u1_grace_cancel + u1_confirm_start) into Hermes' actual
# HOOKS_DIR and writes a per-hook install receipt
bash tools/install_hermes_u1_hooks.sh

# The gateway only discovers hooks at startup — restart it, then check
# files + receipts + (when the gateway log is readable) actual load:
hermes gateway restart
bash tools/install_hermes_u1_hooks.sh --verify

# Per environment — point the gate at the notify script
export U1_GRACE_NOTIFY_CMD=/absolute/path/to/tools/u1_grace_notify_hermes.sh

# REQUIRED for Hermes users: the gate spawns the notify script in a
# stripped subprocess env where `hermes` is usually NOT on PATH. Without
# this, the notify fails (audited as pre_start_grace_notify_failed — the
# wait still runs fail-open, but no Telegram warning is sent).
export HERMES_BIN=/opt/hermes/.venv/bin/hermes
```

The notify script sends a Telegram DM. Reply `CANCEL` (or `STOP` or
`ABORT`, case-insensitive) within the window and the print aborts before
any HTTP call — a bare keyword cancels **every** active grace window;
`cancel <code>` (the code is the last 6 chars of the request id, shown in
the DM) cancels **only** that request, and a code that matches nothing
cancels nothing. Trailing punctuation is fine — `CANCEL!!!` fires (urgency
isn't ambiguity) — but extra words never match: "cancel that plan" is safe
from unintended cancels. Multi-request setups (two concurrent grace windows)
each write their own pending-state file so they don't race each other.

Honesty guard: the DM only promises reply-to-cancel when the notify
receipt shows the hook actually loaded — `--verify` writes that receipt
once the gateway log shows `u1_grace_cancel` came up. Otherwise the DM
gives the SSH `touch <marker>` fallback instead of a reply that would
silently do nothing.

(`tools/install_hermes_cancel_hook.sh` still exists as a pointer that runs
the unified installer — old runbooks keep working.)

Cancelled by mistake, or the bed was actually fine? The refusal payload
carries a `recovery.stage1_command`: the slice and upload are still
valid, so restarting costs one fresh bed photo + one fresh yes — not a
workflow re-run.

Verify the whole chain (including a zero-risk drill that needs no
printer): [docs/verify-cancel-hook.md](docs/verify-cancel-hook.md).

Opt-out for power users at the printer: `U1_GRACE_PERIOD_SECONDS=0`
disables the window. `U1_GRACE_NOTIFY_CMD` unset disables the
notification but the wait still runs (in that case an SSH `touch
<marker>` cancels).

### Model-free start hook (the YES that starts the print)

Starting a print is the same trick in the other direction. At the
bed-clear prompt the workflow arms a per-request window at
`<pending-confirm dir>/<request_id>.json` (the shared `u1_pending`
resolver; `<tempdir>/u1_pending/confirm` by default) and emits NO start
command —
the agent model is handed nothing it could fire. The `u1_confirm_start`
gateway hook redeems the operator's actual YES reply (bare `yes` when one
window is armed; `yes <code>` when several are — a bare yes with several
armed refuses and logs, because a start never guesses) by spawning the
confirm command directly from the gateway process. Every downstream check
(single-use token, nonce, revision + gcode binding, grace window, cancel
hook) is unchanged. Zero LLM in the loop.

The YES is also **bound to the operator**: the armed window records the
operator's platform + user id (auto-resolved from the gateway config), and
the hook refuses a YES from any other sender — or from anyone, when the
binding can't be resolved. Missing identity refuses; a start never guesses.

The failure mode is deliberately boring: **with the hook missing, YES does
nothing — by design.** The window is armed, nothing redeems it, the token
expires, and the printer never starts. Fail-safe, but dead until you
install the hook and restart the gateway. `tools/install_hermes_u1_hooks.sh`
installs it together with the cancel hook (there is no separate installer),
`--verify` reports whether both are actually in place, and
`deploy_to_runtime.sh` prints a loud warning when the receipts are absent.

**An honest boundary statement.** "Handed nothing" is not "can reach
nothing": in the default single-container deployment the gateway and the
agent's terminal tool run as the same Unix user, so a sufficiently
deliberate agent could read the toolkit's own state files and reconstruct
a confirm invocation (concretely: `--confirm-start-for <request_id>`, the
same request-id redemption the gateway hook uses) — the same way it could
touch the printer's API directly. What this design removes is the failure
that actually happened in live testing: an agent firing a start command it
was *given*. What contains the deliberate case is everything downstream:
the invocation is audited with its operator identity, the full gate still
runs, the countdown DM with the one-tap CANCEL still goes to the operator,
and nothing moves for 120 seconds. Removing
the capability domain itself requires running the gateway under a separate
user (or host) from the agent worker so the state files and hook are
simply out of reach — recommended for any deployment that is not a
single-operator home printer, and a good idea even then.


---

[← Back to the README](../README.md)
