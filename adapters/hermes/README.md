# L2 — Hermes form-flow patch

Adds a `form` flow to Hermes that **renders the toolkit's `form_schema` as
native inline buttons in your existing Hermes Telegram chat** — the same bot,
the same conversation, no second token, no sidecar. Mirrors Hermes' own
`clarify` / `exec_approval` / `model_picker` flows exactly.

When this patch is applied to your Hermes install, the kit form arrives as a
real inline-keyboard form (step-by-step screens with toggle multi-select,
pagination, edit-from-review, submit) and submits straight back into
`u1_kit_workflow … --form-answers-json` — same downstream safety gates as the
typed path.

> **The toolkit's own code never imports any of this.** This directory is
> consumer-side, applied to a Hermes install separately.

## What lives here

| File | Role |
|------|------|
| `tools/form_gateway.py` | Gateway-side blocking primitive (mirrors `tools/clarify_gateway.py`). Thread-safe registry of pending forms, `register()` / `wait_for_response()` / `resolve_gateway_form()` / `clear_session()` / `get_form_timeout()`. |
| `tools/form_tool.py` | LLM-facing tool definition. Registers `name="form"` via `tools.registry.registry.register(...)` (Hermes' own plugin entry point — anything dropped in `tools/` is auto-discovered). Schema description + thin dispatcher to a platform-provided callback. |
| `patches/` *(next)* | Anchor-based edits for `gateway/run.py` (`agent.form_callback = …`) and `gateway/platforms/telegram.py` (`send_form` method + form callback-data routing). |
| `install.py` *(next)* | Idempotent installer: copy the two `tools/` files, copy `u1_form_telegram.py` (the L1 renderer this patch's `send_form` calls), apply the two file edits, verify imports. |

## Status

- ✅ `tools/form_gateway.py` — written, mirrors clarify_gateway in pattern + thread safety.
- ✅ `tools/form_tool.py` — written, registers via Hermes' tool registry.
- ⏳ Telegram adapter `send_form` + callback routing — the next file.
- ⏳ `gateway/run.py` wiring (`agent.form_callback`) — the next edit.
- ⏳ `install.py` — applies the above to a target Hermes install.
- ⏳ Live verification in Brent's chat.

## Why this shape

Hermes already does inline-button flows for `clarify`, `exec_approval`, model
picker, and slash confirms (`gateway/platforms/telegram.py`: `send_clarify`,
`send_exec_approval`, `send_model_picker`, `send_slash_confirm`). Adding a
fifth — `send_form` — is the *same* shape as those four. The L1 renderer
under `adapters/telegram/u1_form_telegram.py` is what `send_form` calls; no
duplication.

## Upgrade story (the treadmill, acknowledged)

This is a local patch against Hermes' source files. Re-apply after each
Hermes upgrade by re-running `install.py`. The installer is idempotent and
anchor-based — it detects whether the patch is already applied and only edits
files that need it. If Hermes changes the anchor strings in a future release,
the installer fails cleanly with a clear message rather than corrupting the
source. Long-term: send as an upstream PR (same diff applies cleanly).
