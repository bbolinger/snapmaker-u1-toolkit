# Snapmaker U1 last-layer photo watcher — 2026-06-21

the operator wanted a signal/photo before the U1 bed drops at print completion, because once the print finishes the bed lowers and the camera can no longer show the finished object well.

Implemented helper:

```text
<scripts-dir>/u1_last_layer_watch.py
```

Behavior:

- read-only printer status polling via Moonraker
- no movement, heating, G-code, start, pause, cancel, or file mutation on the printer
- triggers a fresh camera image through `<scripts-dir>/u1_camera.py photo`
- fires only when `print_stats.state == printing`, virtual SD is active, not paused, and current layer is within `LAYER_WINDOW=1` of total layer
- writes state to:

```text
<data-dir>/snapmaker_u1/last_layer/last_layer_watch_state.json
```

- saves timestamped photos under:

```text
<data-dir>/snapmaker_u1/last_layer/
```

- suppresses duplicate notifications per `filename|total_layer`
- prints nothing unless it should notify; designed for Hermes `cronjob(no_agent=True)` silent watchdog behavior
- notification includes `MEDIA:<data-dir>/snapmaker_u1/last_layer/<timestamp>_layer_<n>_of_<total>_<file>.jpg`

Cron job created:

```text
name: u1-last-layer-photo
job_id: 41a1ed6204a7
schedule: every 1m
deliver: origin
script: u1_last_layer_watch.py
no_agent: true
```

Verification during the globe print:

```text
current_layer: 51
total_layer: 812
state: printing
progress: 0.1
```

Script correctly stayed silent and updated state because the print was not yet near the final layer.

If this becomes noisy or obsolete, list cron jobs and remove `41a1ed6204a7` or the current job named `u1-last-layer-photo`.
