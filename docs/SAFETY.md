# Safety model — concrete details

The high-level model is in the [README](../README.md#safety-model). This is the per-action breakdown, the test-operator fence, and the pre-start grace period with the model-free Telegram cancel.

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

Install:

```bash
# One-time — installs the hook into Hermes' actual HOOKS_DIR + restarts gateway
bash tools/install_hermes_cancel_hook.sh

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

Honesty guard: the DM only promises reply-to-cancel when the installer's
receipt file shows the hook actually loaded — otherwise it gives the SSH
`touch <marker>` fallback instead of a reply that would silently do
nothing.

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


---

[← Back to the README](../README.md)
