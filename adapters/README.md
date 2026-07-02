# Reference adapters — form-protocol renderers

> **Status (v2.2):** the `form_schema` event IS now emitted —
> `--interaction-mode form` (or `U1_INTERACTION_MODE=form`) replaces the
> staged turns with one consolidated form. The Hermes integration is a
> **first-party plugin** (`adapters/hermes/plugin/`, deployed by
> `adapters/hermes/install.py`) wired for the **file handoff**: on Submit
> the gateway writes `<U1_FORM_ANSWERS_DIR>/<form_id>.json` and the agent
> relays the emitted `--form-answers-from` command verbatim — answer
> content never passes through the model in either direction. The staged
> text flow remains the default. Discord remains a reference renderer
> (submits via `--form-answers-json`); live end-to-end validation on real
> hardware is the remaining step before this loses its experimental label.

Optional, consumer-side reference code that renders the toolkit's `form_schema`
(emitted on the `kit_form` event) as native UI on a chat surface, collects the
operator's choices, and hands them to the workflow — preferred path: the
gateway-written answers file redeemed via
`u1_kit_workflow.py … --form-answers-from <form_id>`.

**The core toolkit never imports these and has zero platform-SDK dependencies.**
Each adapter is self-contained, installed/run separately, and is NOT part of the
safety core. A consumer can use one of these, copy it, or ignore it and fall
back to the schema's always-present `text_fallback` + `--form-answers '<line>'`.

## Capability ladder

| Level | What | Where |
|-------|------|-------|
| **L0 — text fallback** | typed one-line answer, parsed by `u1_form.parse_answers` | toolkit core (always on) |
| **L1 — pure Python renderer** | step-by-step state machine + keyboard builder, SDK-free, vendorable | `telegram/u1_form_telegram.py`, `discord/u1_form_discord.py` |
| **L2 — host-agent integration** | the agent's *existing* chat bot renders the form natively using the L1 renderer (e.g. a Hermes `send_form` mirroring its existing `send_clarify` / `send_exec_approval` flows) | implementation lives in your agent / a small patch script for it; see `adapters/hermes/` once landed |

L2 is the right answer when the host agent already owns the chat bot — your
existing bot renders the form, no second token, no two-chat shuffle. The L1
renderer is exactly what L2 calls; nothing to throw away by going L2.

## Telegram

### L1 — pure renderer (the library)
Step-by-step flow: **parts → orient → tool → material → profile (paginated) →
supports → action → review card** (parsed echo + Edit / Submit). No SDK at
import — `python-telegram-bot` is loaded lazily only by `run_form_bot()`.

```python
import u1_form_telegram as tg
form = tg.new_form(schema)
screen = tg.render_screen(form)           # {text, keyboard rows}
# render via your bot's SDK, then on each callback:
ev = tg.apply_callback(form, callback_data)
if ev["kind"] == "submit":
    answer = ev["answer"]                 # ready for --form-answers-json
```

Stale-callback safe (out-of-range field/option indices return a clean
`{"kind": "rerender", "warning": ...}` instead of raising). Callback data
fits inside Telegram's 64-byte cap for every renderable screen.

## Discord

`discord/u1_form_discord.py` — pure component builders (native `min_values`/`max_values`
select menus; 25-option cap flagged) + lazy `discord.py` runtime sketch. Same
contract: collect → produce JSON → caller invokes `--form-answers-json`.

## Why this lives here

The form-protocol is platform-neutral by design: the toolkit emits a schema,
consumers render. Shipping reference renderers gives any bot author or host
agent a working starting point on either of the two primary surfaces — without
forcing the core toolkit to take a platform-SDK dependency. The contract is
what `u1_form.build_form_schema` emits and `u1_form.parse_answers_json`
accepts (see `scripts/u1_form.py`).
