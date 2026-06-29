# Reference adapters — form-protocol renderers

Optional, consumer-side reference code that renders the toolkit's `form_schema`
(emitted on the `kit_form` event) as native UI on a chat surface, collects the
operator's choices, and submits them back via
`u1_kit_workflow.py … --form-answers-json '<json>'`.

**The core toolkit never imports these and has zero platform-SDK dependencies.**
Each adapter is self-contained, installed/run separately, and is NOT part of the
safety core. A consumer can use one of these, copy it, or ignore it and fall
back to the schema's always-present `text_fallback` + `--form-answers '<line>'`.

| Adapter | Native control | Install |
|---------|----------------|---------|
| `telegram/` | inline keyboards (toggle ✔ multi-select + Done) | `pip install -r adapters/telegram/requirements.txt` |
| `discord/`  | string select menus (native min/max_values multi-select) | `pip install -r adapters/discord/requirements.txt` |

Each module exposes a **pure core** (no SDK needed, unit-tested):
- Telegram: `field_keyboard`, `apply_callback`, `answer_json`, `new_state`
- Discord: `build_components`, `answer_json`

…and a thin `run_form_bot()` reference runtime that wires the pure core to the
platform SDK (guarded import). See `docs/FORM-PROTOCOL.md` for the contract.
