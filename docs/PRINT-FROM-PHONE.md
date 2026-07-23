# How to Print to a Snapmaker U1 From Your Phone

Snapmaker U1 Toolkit lets you send an STL, 3MF, or ZIP through Telegram, slice it locally with OrcaSlicer, review the plate and settings, verify the printer with a fresh camera image, and approve the print from your phone. The printer stays on your LAN, and the workflow does not start a print until you explicitly approve the exact prepared job.

[← Back to the README](../README.md)

## What the phone workflow includes

```text
Phone → Telegram → local host → OrcaSlicer → previews → Moonraker upload
                                                      ↓
                           fresh bed photo → your YES → safety gate → U1
```

The Telegram chat is the interface, not the safety controller. Deterministic toolkit scripts handle the model, profiles, G-code, printer state, camera checks, upload, approval token, and print monitoring. Hermes and the local LLM relay the workflow's choices and artifacts.

## Before you begin

You need:

- A Snapmaker U1 reachable from a Linux or WSL host on the same LAN.
- Python 3.9 or newer.
- Upstream OrcaSlicer 2.4.0 or newer.
- A working Hermes gateway connected to a private Telegram chat.
- Your numeric Telegram user ID in Hermes' `TELEGRAM_ALLOWED_USERS` configuration.

Native Windows can run the core toolkit, but the current Hermes deployment scripts target Linux paths. See [Windows setup](WINDOWS.md) if the host is Windows.

## 1. Install and verify the core toolkit

```bash
git clone https://github.com/bbolinger/snapmaker-u1-toolkit.git
cd snapmaker-u1-toolkit
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `SNAPMAKER_U1_HOST` to the U1's LAN IP, then run the read-only checks:

```bash
python3 tools/fetch_snapmaker_profiles.py
python3 scripts/u1_slice_workflow.py --help
python3 scripts/snapmaker_u1_status.py
```

Follow the [main installation guide](../README.md#install) if a dependency or network check fails.

## 2. Prove slicing and upload-only mode first

Before involving Telegram, prepare one model from the command line without starting the printer:

```bash
python3 scripts/u1_slice_workflow.py model.3mf \
  --tool T1 --material PETG --orient auto \
  --profile 0_20_strength_snapmaker_u1_0_4_nozzle \
  --supports auto --upload-only --yes
```

List the profile slugs available on your host with:

```bash
python3 scripts/u1_profile_picker.py
```

`--upload-only` sends the prepared G-code to Moonraker storage with printing disabled. It is the safest end-to-end setup test.

## 3. Add the toolkit to Hermes

Run these commands on the Linux host or inside the Hermes container. Let each command finish before starting the next one.

```bash
hermes skills install bbolinger/snapmaker-u1-toolkit/skills/3d-printer-slicing-automation
bash deploy_to_runtime.sh
python3 adapters/hermes/install.py
bash tools/install_hermes_u1_hooks.sh
```

Restart the Hermes gateway from a separate terminal, then verify the operator hooks:

```bash
hermes gateway restart
bash tools/install_hermes_u1_hooks.sh --verify
```

If more than one Telegram user is allowed, bind approvals to the intended operator in the runtime `.env`:

```bash
U1_OPERATOR_BINDING=telegram:<your-numeric-telegram-user-id>
```

See [Snapmaker U1 Telegram setup](TELEGRAM-SETUP.md) for the integration details and failure checks.

## 4. Send a model from your phone

In the private Telegram chat connected to Hermes:

1. Attach one `.stl` or `.3mf` file, or a `.zip` containing multiple STL parts.
2. Choose the parts, orientation, toolhead, material, profile, supports, and action when prompted.
3. Review the plate preview, 3D toolpath view, and generated `review.md` settings summary.
4. Inspect the fresh bed-camera photo.
5. Reply `YES` only if the bed is clear and the prepared job is correct.
6. Use the countdown's **CANCEL** button or reply `CANCEL` if you need to stop the pending start.

The approval is single-use and bound to the operator, request revision, and G-code hash. A changed or stale plan is refused instead of started.

## 5. Monitor the print remotely

The toolkit can send first-layer, last-layer, pause/resume, and completed-print photos through Telegram. Monitoring runs as ordinary scheduled scripts and does not require an LLM turn.

The full jobs and cadence are documented under [Always-on print monitoring](../README.md#always-on-print-monitoring--no-agent-required).

## Common setup problems

- **No printer status:** confirm `SNAPMAKER_U1_HOST` and Moonraker port `7125` are reachable from the host.
- **No profiles:** run `tools/fetch_snapmaker_profiles.py`; extracting profiles from successful printer history is also recommended.
- **Wrong material or layer height in G-code:** follow the [headless OrcaSlicer profile-loading checks](HEADLESS.md#headless-profile-loading-pitfall-read-this).
- **Telegram shows no native form:** rerun `python3 adapters/hermes/install.py`, restart the gateway, and verify that both Hermes plugins loaded.
- **YES does nothing:** rerun `bash tools/install_hermes_u1_hooks.sh --verify`. Do not bypass the failed gate or call Moonraker directly.

For detailed diagnostics, see [TROUBLESHOOTING.md](../TROUBLESHOOTING.md).

---

[Install](../README.md#install) · [Telegram setup](TELEGRAM-SETUP.md) · [Headless OrcaSlicer](HEADLESS.md) · [Safety model](SAFETY.md)
