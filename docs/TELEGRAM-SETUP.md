# Snapmaker U1 Telegram Bot Setup

Use your existing Hermes Telegram chat to send models to Snapmaker U1 Toolkit, answer the print form with native buttons, receive previews and camera photos, and approve a prepared print. The integration uses the same Hermes bot and conversation; it does not require a second Telegram bot token.

[← Back to the README](../README.md)

## How Telegram fits into the workflow

Hermes owns the Telegram connection. The toolkit adds three pieces:

1. The `3d-printer-slicing-automation` skill teaches Hermes to relay toolkit events and commands.
2. The `u1-form` and `snapmaker_u1` plugins render native form buttons and attach previews, reviews, and photos.
3. The YES/CANCEL gateway hooks redeem a bound approval or cancel a pending start without asking the model to control the printer.

The core toolkit remains usable from the CLI without Telegram or Hermes.

## Install the Hermes integration

From a complete toolkit checkout on Linux, WSL, or inside the Hermes container:

```bash
hermes skills install bbolinger/snapmaker-u1-toolkit/skills/3d-printer-slicing-automation
bash deploy_to_runtime.sh
python3 adapters/hermes/install.py
bash tools/install_hermes_u1_hooks.sh
```

The installer adds both required plugins. It should finish with a `[6/6]` verification line that reports the expected hooks.

Restart the gateway from a separate terminal outside the active Hermes chat:

```bash
hermes gateway restart
bash tools/install_hermes_u1_hooks.sh --verify
```

The gateway cannot safely restart itself from inside its own process. If you run the restart from the active Hermes conversation, the command can end before the new hooks load.

## Bind approvals to your Telegram account

Keep the printing workflow in a private direct message. Hermes should already restrict the Telegram gateway with `TELEGRAM_ALLOWED_USERS`.

If exactly one Telegram user is allowed, the toolkit resolves the operator automatically. If multiple users are allowed, add this to the runtime `.env`:

```bash
U1_OPERATOR_BINDING=telegram:<your-numeric-telegram-user-id>
```

Restart the gateway after changing the binding.

## Verify the setup

Check these signals before sending a real print:

- `bash tools/install_hermes_u1_hooks.sh --verify` succeeds.
- Hermes loads both `u1-form` and `snapmaker_u1` plugins.
- A model attachment triggers the Snapmaker U1 workflow.
- The decision form renders as Telegram buttons, or falls back to the documented typed form.
- Plate previews and `review.md` arrive as attachments.
- The bed-clear question includes a fresh U1 camera photo.
- Replying `NO` keeps the G-code uploaded without starting the printer.

Use `NO` for the first live integration test. It exercises attachment routing, slicing, preview generation, Moonraker upload, and the approval path without starting the printer.

## What happens when you reply YES

Hermes does not receive a print-start command. The gateway consumes the YES, checks the bound operator and current request, and invokes the toolkit's approval gate. A countdown message appears only if those checks succeed.

If no countdown appears, the print was not started. Run the hook verification and inspect the reported refusal; do not retry by calling Moonraker directly.

## Native buttons versus the text fallback

The first-party Hermes plugin renders parts, orientation, tool, material, profile, supports, advanced settings, and action as native Telegram controls. On a host without the form plugin, the workflow can emit a typed one-line fallback instead. Both paths are parsed and validated by toolkit code rather than interpreted by the model.

Developers integrating a different bot can use the SDK-free reference renderer in [`adapters/telegram/u1_form_telegram.py`](../adapters/telegram/u1_form_telegram.py) and the [adapter contract](../adapters/README.md).

## Troubleshooting

- **No form buttons:** rerun `python3 adapters/hermes/install.py`, then restart the gateway externally.
- **Attachments do not appear:** verify the `snapmaker_u1` hook plugin loaded after the restart.
- **YES has no effect:** rerun `bash tools/install_hermes_u1_hooks.sh --verify`.
- **Wrong user is refused:** check `TELEGRAM_ALLOWED_USERS` and `U1_OPERATOR_BINDING`.
- **CANCEL is not acknowledged:** the safe fallback can only cancel; follow the recovery steps in the main Hermes integration documentation.

See [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) and the full [Hermes integration](../README.md#hermes-integration) for deeper diagnostics.

---

[Print from your phone](PRINT-FROM-PHONE.md) · [Install](../README.md#install) · [Safety model](SAFETY.md)
