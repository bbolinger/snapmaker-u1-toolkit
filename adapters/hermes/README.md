# L2 — Hermes u1-form plugin

Adds a `form` flow to Hermes that **renders the toolkit's `form_schema` as
native inline buttons in your existing Hermes Telegram chat** — the same bot,
the same conversation, no second token, no sidecar. Mirrors Hermes' own
`clarify` / `exec_approval` / `model_picker` flows exactly.

When installed, the kit form arrives as a real inline-keyboard form
(step-by-step screens with toggle multi-select, pagination, edit-from-review,
submit). On Submit the **gateway** writes the answers file the workflow
redeems via `--form-answers-from` — answer content never rides through the
model in either direction. Same downstream safety gates as the typed path.

> **The toolkit's own code never imports any of this.** This directory is
> consumer-side, applied to a Hermes install separately.

## Architecture (why a plugin)

Registering a tool is not the same as the model being **offered** it.
Platform agents get a per-toolset allowlist resolved by
`hermes_cli.tools_config._get_platform_tools`, and on a bare-composite
config (`platform_toolsets.telegram: [hermes-telegram]`) built-in toolsets
are enabled by **subset-inference** against the composite:

- a runtime-registered toolset (`form`) is never a subset → never offered;
- joining an existing toolset is worse: `form` in `clarify` makes
  `{clarify, form} ⊄ hermes-telegram` and **evicts clarify itself**
  (verified against a live config, and pinned by
  `tests/test_hermes_real_package.py`).

Hermes' first-party door is the **plugin system**: toolsets provided by an
enabled plugin are auto-enabled per platform — no subset inference, no
effect on built-in toolsets, and the operator can toggle `form` in
`hermes tools` like any other toolset.

Two more load-bearing facts, both proven against hermes-agent 0.18 source:

1. **Registry dispatch carries no callback.** Generic tool handlers receive
   only `(task_id, session_id, user_task)` — only hardcoded tools like
   `clarify` get `agent.clarify_callback` in the executor. So the gateway's
   `run.py` patch publishes its per-turn form callback into
   `tools.form_gateway` **keyed by `agent.session_id`** (the exact value
   dispatch passes to handlers), and the plugin's handler looks it up there.
2. **The adapter class exists twice.** The Telegram adapter file loads under
   two module names (`hermes_plugins.platforms__telegram.adapter` via the
   plugin loader the gateway uses; `plugins.platforms.telegram.adapter` as a
   namespace-package import) — two distinct class objects. Patching by
   import can land on the copy the gateway never instantiates. The plugin's
   `pre_gateway_dispatch` hook therefore patches `type()` of the **live
   adapter instances** in `gateway.adapters` — by construction the class the
   gateway dispatches through, installed before the message that could
   trigger a form is dispatched.

## What lives here

| File | Role |
|------|------|
| `plugin/plugin.yaml` | Hermes plugin manifest (`kind: standalone`; user plugins are opt-in via `hermes plugins enable u1-form`). |
| `plugin/__init__.py` | Plugin entry point: `register(ctx)` registers the `form` tool (own `form` toolset — surfaced by the plugin path, see above) and the `pre_gateway_dispatch` hook. Also the tool core: schema, answer shaping, and the session-keyed callback lookup. |
| `plugin/telegram_patch.py` | `ensure_patched(cls)` — class-level `send_form` + form callback routing applied to the live adapter class handed in by the hook. Callback ownership is matched by `(chat_id, message_id)`, so Hermes' own callbacks (`cl:`, `ea:`, `mp:`, `gt:`…) always fall through untouched. Carries the gateway-side answers-file writer (`U1_FORM_ANSWERS_DIR`). |
| `tools/form_gateway.py` | Gateway-side blocking primitive (mirrors `tools/clarify_gateway.py`): thread-safe registry of pending forms + the session-keyed form-callback registry that bridges registry dispatch back to the gateway. Copied into Hermes' `tools/` (run.py imports it by that stable path). |
| `install.py` | Idempotent installer: copies `form_gateway.py`, deploys the plugin to `<HERMES_HOME>/plugins/u1-form/` (renderer included), enables it, applies the single anchor-based `gateway/run.py` edit, removes pre-plugin layout files from earlier v2.2-dev deploys, and **verifies the real invariant** in the venv: on a bare-composite config both `clarify` and `form` must resolve. `--dry-run` and `--uninstall` supported; re-runs replace the marked run.py block in place when its body changed. |

The L1 renderer `u1_form_telegram.py` is **single-sourced** from the sibling
[`adapters/telegram/`](../telegram/) directory — this tree keeps no copy.
`install.py` copies it into the deployed plugin dir at install time, so run
the installer from a full checkout of the toolkit repo (not from a
copied-out `hermes/` directory alone).

## Verification

- Hermetic: `tests/test_adapters.py` (plugin registration, handler bridge,
  live-class hook patching, answers writer, callback registry, installer).
- Against the real package: `tests/test_hermes_real_package.py` — baseline
  bug reproduction, the no-eviction superset invariant
  (`with_plugin == baseline | {"form"}`), schema delivery through
  `get_tool_definitions`, clean load under the real plugin manager, and the
  run.py patcher against the real `gateway/run.py`. Gated on
  `U1_HERMES_AGENT_SRC` (see the module docstring for how to point it at an
  unzipped wheel).

## Upgrade story

The plugin itself survives Hermes upgrades untouched (`~/.hermes/plugins/`
is user data). Two pieces are still patch-shaped and re-applied by re-running
`install.py` after an upgrade: the `tools/form_gateway.py` copy and the
anchor-based `gateway/run.py` edit. The installer is idempotent and
marker-guarded — it refuses cleanly if Hermes changes the clarify-wiring
anchor rather than corrupting the source, and `--uninstall` skips the run.py
restore when pip already replaced the file (never downgrades an upgraded
Hermes). Long-term: upstream a first-party `send_form` + form-callback hook
so the last two patches can retire.
