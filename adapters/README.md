# Reference adapters — form-protocol renderers

Optional, consumer-side reference code that renders the toolkit's `form_schema`
(emitted on the `kit_form` event) as native UI on a chat surface, collects the
operator's choices, and submits them back via
`u1_kit_workflow.py … --form-answers-json '<json>'`.

**The core toolkit never imports these and has zero platform-SDK dependencies.**
Each adapter is self-contained, installed/run separately, and is NOT part of the
safety core. A consumer can use one of these, copy it, or ignore it and fall
back to the schema's always-present `text_fallback` + `--form-answers '<line>'`.

## Capability ladder

| Level | What | Where |
|-------|------|-------|
| **L0 — text fallback** | typed one-line answer, parsed by `u1_form.parse_answers` | toolkit core (always on) |
| **L1 — pure Python renderer** | step-by-step state machine + keyboard builder, SDK-free, vendorable | `telegram/u1_form_telegram.py`, `discord/u1_form_discord.py` |
| **L2 — host-agent integration** | the agent's Telegram adapter renders the form natively via the L1 renderer (e.g. a Hermes upstream PR adding `form_callback` + `send_form`) | upstream PR; preferred long-term answer |
| **L3 — sidecar bot (escape hatch)** | a SEPARATE Telegram bot (own token) that runs the L1 renderer alongside your main agent — two bots in the chat, UX tax acknowledged | `telegram/sidecar.py`; use when L2 is blocked |

## Telegram

### L1 — pure renderer (the library)
Step-by-step flow: parts → orient → tool → material → profile (paginated) →
supports → action → **review card** (parsed echo + Edit/Submit). No SDK at
import — `python-telegram-bot` is loaded lazily only by `run_form_bot()`.

```python
form = tg.new_form(schema)
screen = tg.render_screen(form)           # {text, keyboard rows}
# render via your bot's SDK, then on each callback:
ev = tg.apply_callback(form, callback_data)
if ev["kind"] == "submit":
    answer = ev["answer"]                 # ready for --form-answers-json
```

### L3 — sidecar bot (`telegram/sidecar.py`)
A standalone process that gives you live buttons today without modifying your
main agent. Two bots in the chat is the tax — use this as the escape hatch
while L2 is in flight.

**One-time setup**
1. Create a **second** Telegram bot via [@BotFather](https://t.me/BotFather):
   `/newbot` → pick a display name → get the token.
2. Install deps:
   ```
   pip install -r adapters/telegram/requirements.txt
   ```
3. Set env:
   ```
   export U1_SIDECAR_BOT_TOKEN='123:abc…'                # from BotFather
   export U1_SIDECAR_BOT_USERNAME='your_sidecar_bot'     # without leading @
   export U1_SIDECAR_ALLOWLIST='123456789,987654321'     # CSV of Telegram user ids
   # Optional — defaults match the deployed toolkit layout:
   #   U1_SCRIPTS_DIR=/opt/data/scripts
   #   SNAPMAKER_U1_DATA_DIR=/opt/data/snapmaker_u1
   ```
4. Run:
   ```
   python3 adapters/telegram/sidecar.py
   ```
   Long polling — no HTTPS, no public hosting, no Mini-App config.

**How the flow looks**
- The kit workflow emits a `form_url` in the `kit_form` event when
  `U1_SIDECAR_BOT_USERNAME` is set:
  `https://t.me/<sidecar_bot>?start=<request_id>`
- Your main agent (Hermes/Gemma) posts that URL in chat — Telegram renders it
  as a tappable link.
- Operator taps → Telegram opens the sidecar DM and sends
  `/start <request_id>` automatically → sidecar loads the request, walks the
  operator through screens, and on **Submit** invokes
  `u1_kit_workflow.py … --form-answers-json`.
- Sidecar tells the operator to return to the main chat for the Stage-1
  bed-photo gate. **Safety pipeline unchanged** — buttons collect only; every
  downstream gate (slicer warnings, readiness card, Stage-1 photo + token,
  `can_start()`) runs identically to the typed-form path.
- Allowlist (`U1_SIDECAR_ALLOWLIST`) is fail-closed: without it, the bot
  refuses every user.

## Discord

`discord/u1_form_discord.py` — pure component builders (native `min_values`/`max_values`
select menus; 25-option cap flagged) + lazy `discord.py` runtime sketch. Same
contract: collect → produce JSON → caller invokes `--form-answers-json`.

## Why this lives here

The form-protocol is platform-neutral by design: the toolkit emits a schema,
consumers render. Shipping reference renderers + a sidecar gives any user a
working starting point on either of the two primary surfaces — without forcing
the core toolkit to take a platform-SDK dependency. See `docs/FORM-PROTOCOL.md`
(internal design doc) for the contract.
