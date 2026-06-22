# Snapmaker U1 camera refresh via Moonraker websocket

## Trigger

When `monitor.jpg` exists but is stale, do not assume the U1 camera cannot be refreshed from Hermes. The U1 exposes Snapmaker-specific camera methods over Moonraker's websocket JSON-RPC transport.

Use Moonraker's API key endpoint if LAN auth permits it:

```bash
curl -sS http://PRINTER_IP:7125/access/api_key
```

Open websocket:

```text
ws://PRINTER_IP:7125/websocket?token=API_KEY
```

Send:

```json
{
  "jsonrpc": "2.0",
  "method": "camera.start_monitor",
  "params": {
    "domain": "lan",
    "interval": 0
  },
  "id": 1001
}
```

Expected notification includes:

```json
{
  "method": "notify_camera_status_change",
  "params": [
    {
      "monitor_domain": "lan",
      "monitoring": true,
      "timestamp": "YYYY-MM-DD HH:MM:SS"
    }
  ]
}
```

Then wait a couple seconds and fetch the refreshed file:

```text
http://PRINTER_IP:7125/server/files/camera/monitor.jpg
```

Verify freshness via:

```text
/server/files/list?root=camera
```

The entry for `monitor.jpg` should have a current `modified` timestamp before using the image as bed-clear evidence.

## What *not* to rely on

The usual Fluidd/OctoPrint webcam proxy paths may fail on the U1 even when the camera method works:

```text
/webcam/?action=snapshot
/webcam/?action=stream
```

Observed behavior in one U1 session: `/webcam/?action=snapshot` returned 404/502 while websocket `camera.start_monitor` successfully refreshed `/server/files/camera/monitor.jpg`.

## Source-code/research anchors

Useful public repos that document or implement the method:

- `evansmike881/Home-Assistant---Snapmaker-Camera-Keepalive`
  - Periodically sends `camera.start_monitor` to keep U1 camera awake.
- `PrintsNCode/Anycubic-Snapmaker-remote-cam-lan`
  - Same websocket keepalive approach.
- `paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware`
  - Firmware-side implementation includes:
    - `camera.start_monitor`
    - `camera.stop_monitor`
    - `camera.take_a_photo`
    - `camera.detect_capture`

In the extended firmware implementation, `camera.start_monitor` sets a snapshot interval, ensures a `monitor.jpg` symlink, returns `{"state":"success","url":"/files/camera/monitor.jpg"}`, and emits `notify_camera_status_change`.

## Workflow lesson

For U1/Snapmaker capabilities, scrub U1-specific open-source/community repos before concluding an action is impossible. Generic Moonraker/Fluidd docs may miss Snapmaker-specific websocket methods.

Still keep the physical-device safety stance: read-only/status/image refresh is acceptable discovery; movement, heating, printing, deletion, shell commands, and G-code remain approval-gated.