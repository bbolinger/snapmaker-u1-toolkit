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
| `tools/form_tool.py` | LLM-facing tool definition. Registers `name="form"` via `tools.registry.registry.register(...)` (Hermes' own plugin entry point — anything dropped in `tools/` is auto-discovered). Schema description + thin dispatcher to a platform-provided callback. Also carries the class-level monkey-patch of the Telegram platform class (`send_form` + form callback routing — no Hermes source edits). Version-adaptive: resolves `plugins.platforms.telegram.adapter.TelegramAdapter` on hermes-agent ≥ 0.18 (the plugin-adapter refactor), falling back to `gateway.platforms.telegram.TelegramPlatform` on ≤ 0.17; if neither imports it logs every path it tried and degrades to text fallback. |
| `install.py` | Idempotent installer: copies the two `tools/` files plus `u1_form_telegram.py` (the L1 renderer this patch's `send_form` calls) into Hermes' `tools/`, applies the single anchor-based `gateway/run.py` edit (`agent.form_callback = …`), verifies imports. `--dry-run` and `--uninstall` supported. |

The L1 renderer `u1_form_telegram.py` is **single-sourced** from the sibling
[`adapters/telegram/`](../telegram/) directory — this tree keeps no copy.
`install.py` reads it from `../telegram/u1_form_telegram.py` at install time,
so run the installer from a full checkout of the toolkit repo (not from a
copied-out `hermes/` directory alone).

## Status

- ✅ `tools/form_gateway.py` — written, mirrors clarify_gateway in pattern + thread safety.
- ✅ `tools/form_tool.py` — written, registers via Hermes' tool registry.
- ✅ Telegram adapter `send_form` + callback routing — class-level monkey-patch inside `tools/form_tool.py` (a separate `patches/` directory was never needed).
- ✅ `gateway/run.py` wiring (`agent.form_callback`) — applied by `install.py` (anchor-based, marker-guarded, backup + restore on `--uninstall`).
- ✅ `install.py` — applies the above to a target Hermes install.
- ⏳ Live verification in Brent's chat.
- ⏳ Upstream: the `form_schema` event is not yet emitted by `u1_kit_workflow` (see `adapters/README.md`); this patch is a reference implementation ahead of that wiring.

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
