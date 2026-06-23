#!/usr/bin/env python3
"""Snapmaker U1 camera helper.

Read-only/camera-only helper for the operator's Snapmaker U1.

Subcommands:
  photo       Trigger camera.start_monitor and save a fresh monitor.jpg
  freshness  Print monitor.jpg metadata without triggering capture
  watch      Trigger repeatedly until the file timestamp changes or timeout
  check      Trigger fresh capture and emit a fail-closed bed-check packet for vision review

This intentionally does not send movement/heating/G-code commands.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from u1_config import get_u1_host, get_u1_port, get_data_dir


def _default_output() -> str:
    return str(get_data_dir() / "latest_monitor.jpg")


def http_get(url: str, timeout: float = 10.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def moonraker_base(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def get_api_key(host: str, port: int) -> str:
    payload = json.loads(http_get(f"{moonraker_base(host, port)}/access/api_key", timeout=5).decode())
    return payload["result"]


def camera_metadata(host: str, port: int) -> dict | None:
    payload = json.loads(http_get(f"{moonraker_base(host, port)}/server/files/list?root=camera", timeout=5).decode())
    for item in payload.get("result", []):
        if item.get("path") == "monitor.jpg":
            item = dict(item)
            modified = item.get("modified")
            if isinstance(modified, (int, float)):
                item["modified_iso_utc"] = datetime.fromtimestamp(modified, tz=timezone.utc).isoformat()
            return item
    return None


def send_ws_jsonrpc(host: str, port: int, token: str, payload: dict, timeout: float = 8.0) -> list[dict]:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        request = (
            f"GET /websocket?token={token} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed early")
            response += chunk
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(response[:300].decode("utf-8", errors="replace"))

        message = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(message)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(message)))

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
                    decoded = json.loads(data.decode("utf-8"))
                except json.JSONDecodeError:
                    decoded = {"raw": data.decode("utf-8", errors="replace")}
                # Moonraker can emit noisy proc-stat notifications on the same websocket.
                # Keep only the camera/request messages so CLI output stays operator-readable.
                if decoded.get("id") == payload.get("id") or decoded.get("method") == "notify_camera_status_change":
                    replies.append(decoded)
                    break
        return replies
    finally:
        try:
            sock.close()
        except Exception:
            pass


def start_monitor(host: str, port: int, interval: float) -> list[dict]:
    token = get_api_key(host, port)
    return send_ws_jsonrpc(
        host,
        port,
        token,
        {
            "jsonrpc": "2.0",
            "method": "camera.start_monitor",
            "params": {"domain": "lan", "interval": interval},
            "id": int(time.time() * 1000),
        },
    )


def fetch_monitor(host: str, port: int, output: str) -> dict:
    image = http_get(f"{moonraker_base(host, port)}/server/files/camera/monitor.jpg", timeout=10)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image)
    return {"output": str(out), "bytes": len(image), "jpeg_magic": image[:3] == b"\xff\xd8\xff"}


def command_photo(args: argparse.Namespace) -> dict:
    # Lazy import so this module stays usable on systems without cavity_led
    # configured (the wrap is a no-op then — failures are caught and logged).
    from u1_led import photo_wrap

    with photo_wrap():
        before = camera_metadata(args.host, args.port)
        replies = start_monitor(args.host, args.port, args.interval)
        time.sleep(args.wait)
        fetched = fetch_monitor(args.host, args.port, args.output)
        after = camera_metadata(args.host, args.port)
    return {
        "ok": True,
        "mode": "photo",
        "host": args.host,
        "before": before,
        "after": after,
        "changed": bool(before and after and before.get("modified") != after.get("modified")),
        "websocket_replies": replies,
        **fetched,
    }


def command_freshness(args: argparse.Namespace) -> dict:
    meta = camera_metadata(args.host, args.port)
    return {"ok": meta is not None, "mode": "freshness", "host": args.host, "monitor": meta}


def command_watch(args: argparse.Namespace) -> dict:
    initial = camera_metadata(args.host, args.port)
    last_modified = initial.get("modified") if initial else None
    deadline = time.time() + args.timeout
    attempts = 0
    last_result = None
    while time.time() < deadline:
        attempts += 1
        last_result = command_photo(args)
        after = last_result.get("after") or {}
        if after.get("modified") != last_modified:
            return {"ok": True, "mode": "watch", "attempts": attempts, "initial": initial, "result": last_result}
        time.sleep(args.poll)
    return {"ok": False, "mode": "watch", "attempts": attempts, "initial": initial, "last_result": last_result, "error": "timeout waiting for monitor.jpg timestamp change"}


def command_check(args: argparse.Namespace) -> dict:
    watch_result = command_watch(args)
    photo_result = watch_result.get("result") or watch_result.get("last_result") or {}
    after = photo_result.get("after") or {}
    return {
        "ok": bool(watch_result.get("ok") and photo_result.get("jpeg_magic")),
        "mode": "check",
        "host": args.host,
        "fresh": bool(watch_result.get("ok") and photo_result.get("changed")),
        "image": photo_result.get("output"),
        "monitor": after,
        "policy": {
            "path": str(get_data_dir() / "bed_check_policy.md"),
            "fail_closed": True,
            "allowed_classifications": ["clear", "not_clear", "unknown"],
            "clear_requires": "fresh image plus enough visible build plate to confidently see no obstruction",
            "unknown_if": "partial bed visibility, poor angle, stale image, glare, occlusion, blur, or any uncertainty",
            "physical_actions_require_explicit_operator_approval": True,
        },
        "vision_required": True,
        "vision_prompt": "Classify Snapmaker U1 bed/build-plate clearance as clear, not_clear, or unknown using the policy. Fail closed.",
        "capture": watch_result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Snapmaker U1 camera helper")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    photo = sub.add_parser("photo", help="Trigger camera refresh and fetch monitor.jpg")
    photo.add_argument("--output", default=None)
    photo.add_argument("--wait", type=float, default=2.5)
    photo.add_argument("--interval", type=float, default=0)
    photo.set_defaults(func=command_photo)

    fresh = sub.add_parser("freshness", help="Read monitor.jpg metadata only")
    fresh.set_defaults(func=command_freshness)

    watch = sub.add_parser("watch", help="Retry photo until timestamp changes or timeout")
    watch.add_argument("--output", default=None)
    watch.add_argument("--wait", type=float, default=2.0)
    watch.add_argument("--interval", type=float, default=0)
    watch.add_argument("--timeout", type=float, default=20.0)
    watch.add_argument("--poll", type=float, default=2.0)
    watch.set_defaults(func=command_watch)

    check = sub.add_parser("check", help="Fresh capture plus fail-closed bed-check packet")
    check.add_argument("--output", default=None)
    check.add_argument("--wait", type=float, default=2.0)
    check.add_argument("--interval", type=float, default=0)
    check.add_argument("--timeout", type=float, default=20.0)
    check.add_argument("--poll", type=float, default=2.0)
    check.set_defaults(func=command_check)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # Resolve host/port/output lazily so missing config only fails on run,
    # not at module import.
    args.host = args.host or get_u1_host()
    if args.port is None:
        args.port = get_u1_port()
    if hasattr(args, "output") and args.output is None:
        args.output = _default_output()
    result = args.func(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
