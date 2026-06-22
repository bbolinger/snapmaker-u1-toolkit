# Snapmaker U1 first-pass research

## Source notes

Snapmaker's wiki/FAQ and Snapmaker Orca quick-start indicate:

- U1 supports Wi-Fi and USB flash drive print transfer.
- Snapmaker Orca can bind to the printer in Cloud Mode or LAN Mode.
- Snapmaker Orca can upload G-code or upload-and-print after slicing.
- Snapmaker Orca supports remote monitoring, device management, and device control.
- U1 firmware is described as a custom system built on **Klipper, Moonraker, and Fluidd**.
- Snapmaker recommends Snapmaker Orca for U1 because it includes adapted material profiles/settings; vanilla OrcaSlicer may work but should be treated as a separate validation path.

## Initial control hypothesis

If U1 exposes Moonraker on the LAN, the safest control plane is Moonraker HTTP:

- Read-only status first: `/server/info`, `/printer/info`, `/printer/objects/query`.
- Upload only after status is proven: `/server/files/upload`.
- Start only behind explicit approval: `/printer/print/start`.
- Pause/cancel are physical side effects too; gate or pre-agree.

## MCP candidates found

### `lexfrei/mcp-raker`

GitHub: <https://github.com/lexfrei/mcp-raker>

Best first MCP candidate for U1-like Klipper/Moonraker control.

Pros:
- Focused on Moonraker.
- Broad coverage: status, printing, G-code, files, history, queues, machine info, webcams, Spoolman, database, announcements.
- Destructive admin tools require `MOONRAKER_ENABLE_ADMIN=true` and are disabled by default.
- Supports unauthenticated trusted-LAN Moonraker, API key, JWT, or username/password.
- Ships as a container image.

Hermes config sketch:

```yaml
mcp_servers:
  snapmaker_u1:
    command: "docker"
    args:
      - "run"
      - "--rm"
      - "-i"
      - "-e"
      - "MOONRAKER_URL"
      - "-e"
      - "MOONRAKER_API_KEY"
      - "-v"
      - "mcp-raker-session:/home/nobody/.mcp-raker"
      - "ghcr.io/lexfrei/mcp-raker:latest"
    env:
      MOONRAKER_URL: "http://PRINTER_IP:7125"
```

Do **not** enable `MOONRAKER_ENABLE_ADMIN` for basic print workflows.

### `mikehatch/KlipperMCP`

GitHub: <https://github.com/mikehatch/KlipperMCP>

Good for Klipper config/status work: config reads/searches, macro evaluation, status, temperatures, progress, print control. Better as a config/debug companion than the first minimal upload/start surface.

### `DMontgomery40/mcp-3D-printer-server`

GitHub: <https://github.com/DMontgomery40/mcp-3D-printer-server>

Broad multi-printer MCP with OctoPrint, Klipper/Moonraker, Duet, Repetier, Bambu, Prusa, Creality, STL tools, and slicing utilities. It may be useful later, but its roadmap mentions bringing non-Bambu paths up to stronger feature parity. For U1, prefer focused Moonraker first.

### `codeofaxel/Kiln`

GitHub: <https://github.com/codeofaxel/Kiln>

Ambitious end-to-end design/slice/print MCP. Interesting to watch; not first choice for cautious hardware control.

## Recommended staged test for the operator's U1

1. Identify U1 LAN IP.
2. Run read-only Moonraker probes; do not heat/move/upload.
3. Build or use a minimal status script.
4. Upload a known-good Snapmaker-Orca-generated G-code with `print=false`.
5. Ask for explicit approval before start.
6. Only then test slicer automation with known U1 PETG preset and compare output to GUI slicing.

## Safety conclusion

Risk is manageable with staged testing and approval gates. Risk is too high for blind “STL in → auto-start print” until the control surface, slicer profile fidelity, tool/material mapping, and bed-clear checks are proven.