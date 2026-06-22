#!/usr/bin/env python3
"""Trigger a Snapmaker U1 camera monitor refresh via Moonraker websocket, then fetch monitor.jpg.

No third-party dependencies. Resolves the U1 endpoint via u1_config (env vars, .env, or the JSON config file under the data dir).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from u1_config import get_u1_host, get_u1_port, get_data_dir


def http_get(url: str, timeout: float = 10.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def get_api_key(base: str) -> str:
    data = json.loads(http_get(f"{base}/access/api_key", timeout=5).decode("utf-8"))
    return data["result"]


def send_ws_jsonrpc(host: str, port: int, token: str, payload: dict, timeout: float = 8.0) -> list[dict]:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = f"/websocket?token={token}"
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed early")
            resp += chunk
        status_line = resp.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            raise RuntimeError(f"websocket upgrade failed: {resp[:300]!r}")

        msg = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text frame
        n = len(msg)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))

        replies: list[dict] = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                frame_header = sock.recv(2)
            except socket.timeout:
                break
            if not frame_header:
                break
            b1, b2 = frame_header
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", sock.recv(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", sock.recv(8))[0]
            if b2 & 0x80:
                frame_mask = sock.recv(4)
                data = bytes(c ^ frame_mask[i % 4] for i, c in enumerate(sock.recv(length)))
            else:
                data = sock.recv(length)
            if opcode == 1 and data:
                try:
                    replies.append(json.loads(data.decode("utf-8")))
                except json.JSONDecodeError:
                    replies.append({"raw": data.decode("utf-8", errors="replace")})
            if any(r.get("id") == payload.get("id") or r.get("method") == "notify_camera_status_change" for r in replies):
                break
        return replies
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main() -> int:
    p = argparse.ArgumentParser(description="Trigger and fetch a fresh Snapmaker U1 camera monitor image")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--output", default=None)
    p.add_argument("--wait", type=float, default=2.5, help="seconds to wait after start_monitor before fetching")
    p.add_argument("--interval", type=float, default=0, help="camera.start_monitor interval parameter")
    args = p.parse_args()

    host = args.host or get_u1_host()
    port = args.port if args.port is not None else get_u1_port()
    # Push resolved values back into args so the downstream `args.host` /
    # `args.port` references (websocket call + summary) use the resolved
    # values rather than the raw argparse Nones. (Hermes finding F6.)
    args.host = host
    args.port = port
    base = f"http://{host}:{port}"
    output = Path(args.output) if args.output else get_data_dir() / "latest_monitor.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)

    before = None
    try:
        listing = json.loads(http_get(f"{base}/server/files/list?root=camera", timeout=5).decode("utf-8"))["result"]
        for item in listing:
            if item.get("path") == "monitor.jpg":
                before = item
                break
    except Exception:
        pass

    token = get_api_key(base)
    replies = send_ws_jsonrpc(
        args.host,
        args.port,
        token,
        {
            "jsonrpc": "2.0",
            "method": "camera.start_monitor",
            "params": {"domain": "lan", "interval": args.interval},
            "id": int(time.time()),
        },
    )
    time.sleep(args.wait)

    image = http_get(f"{base}/server/files/camera/monitor.jpg", timeout=10)
    output.write_bytes(image)

    after = None
    try:
        listing = json.loads(http_get(f"{base}/server/files/list?root=camera", timeout=5).decode("utf-8"))["result"]
        for item in listing:
            if item.get("path") == "monitor.jpg":
                after = item
                break
    except Exception:
        pass

    result = {
        "ok": True,
        "host": args.host,
        "output": str(output),
        "bytes": len(image),
        "jpeg_magic": image[:3] == b"\xff\xd8\xff",
        "before": before,
        "after": after,
        "websocket_replies": replies,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
