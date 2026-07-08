#!/usr/bin/env python3
"""u1_ollama_bridge — a tiny OpenAI-compatible → Ollama-native shim.

Why this exists
---------------
Ollama's OpenAI-compatible endpoint (``/v1/chat/completions``) intermittently
leaks gemma chat-template tokens (``<|tool_call|>``, ``<|channel|>``, …) into
tool-call responses instead of parsing them — upstream ollama/ollama#15798.
The NATIVE endpoint (``/api/chat``) parses the same tool calls cleanly, every
time. This bridge accepts the ``/v1`` request an OpenAI client (e.g. a Hermes
provider) already sends, forwards it to ``/api/chat``, and re-envelopes the
clean native result back into ``/v1`` shape. The broken serializer is never in
the path.

It is deliberately zero-dependency (Python stdlib only) and self-contained, so
any maker can run it next to their own Ollama without adopting a framework:

    python3 u1_ollama_bridge.py                 # listens :11435 -> 127.0.0.1:11434
    OLLAMA_BASE=http://192.168.1.5:11434 \
    BRIDGE_PORT=11435 python3 u1_ollama_bridge.py

Then point the OpenAI client's base_url at ``http://<bridge-host>:11435/v1``.

Design choices (robustness over cleverness — a fragile stream translator is the
same class of bug this fixes):
  * Only ``POST /v1/chat/completions`` is translated. Everything else is proxied
    to Ollama byte-for-byte, so ``/v1/models`` and native ``/api/*`` still work
    and provider init is unaffected.
  * The upstream call is always NON-streaming: we get the complete, clean tool
    call in one piece, then either return it (client stream=false) or emit it as
    a single well-formed SSE delta + ``[DONE]`` (client stream=true). No
    incremental tool-call-delta reassembly, which is exactly where fragility
    lives.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434").rstrip("/")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "11435"))
UPSTREAM_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "600"))  # model calls are slow

# OpenAI sampling fields that map into Ollama's native `options` block.
_OPT_MAP = {
    "temperature": "temperature", "top_p": "top_p", "top_k": "top_k",
    "seed": "seed", "stop": "stop", "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty", "max_tokens": "num_predict",
    "max_completion_tokens": "num_predict",
}


def _to_native(req: dict) -> dict:
    """OpenAI /v1 chat request -> Ollama /api/chat request (non-streaming)."""
    native = {
        "model": req.get("model"),
        "messages": req.get("messages", []),
        "stream": False,
    }
    if req.get("tools"):
        native["tools"] = req["tools"]
    if req.get("tool_choice") not in (None, "auto"):
        native["tool_choice"] = req["tool_choice"]
    opts = {}
    for oa, ol in _OPT_MAP.items():
        if oa in req and req[oa] is not None:
            opts[ol] = req[oa]
    if opts:
        native["options"] = opts
    # Reasoning: if the client asked for any thinking, let the native model
    # think; the tool call still returns cleanly either way.
    if req.get("reasoning_effort") or req.get("reasoning") or req.get("reasoning_config"):
        native["think"] = True
    return native


def _native_tool_calls_to_v1(tcs):
    """Native tool_calls (arguments is a dict) -> /v1 shape (arguments is a
    JSON string, type:function present)."""
    out = []
    for i, tc in enumerate(tcs or []):
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {})
        out.append({
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
            "index": i,
            "type": "function",
            "function": {"name": fn.get("name"), "arguments": args},
        })
    return out


def _to_v1_response(native: dict, model: str) -> dict:
    """Ollama /api/chat response -> /v1 chat.completion envelope."""
    msg = native.get("message", {}) or {}
    tcs = _native_tool_calls_to_v1(msg.get("tool_calls"))
    v1_msg = {"role": "assistant", "content": msg.get("content") or ""}
    if tcs:
        v1_msg["tool_calls"] = tcs
    finish = "tool_calls" if tcs else (native.get("done_reason") or "stop")
    if finish == "length":
        finish = "length"
    elif finish not in ("tool_calls", "length"):
        finish = "stop"
    pt = native.get("prompt_eval_count") or 0
    ct = native.get("eval_count") or 0
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(native.get("created_ts") or time.time()),
        "model": model,
        "choices": [{"index": 0, "message": v1_msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": pt + ct},
    }


def _v1_to_stream_chunks(v1: dict):
    """Turn a finished /v1 response into a minimal, valid OpenAI SSE stream:
    one delta chunk carrying the whole message, one finish chunk, then DONE."""
    cid, model, created = v1["id"], v1["model"], v1["created"]
    ch = v1["choices"][0]
    delta = {"role": "assistant"}
    if ch["message"].get("content"):
        delta["content"] = ch["message"]["content"]
    if ch["message"].get("tool_calls"):
        delta["tool_calls"] = ch["message"]["tool_calls"]

    def frame(d, finish):
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": d, "finish_reason": finish}],
        }) + "\n\n"
    yield frame(delta, None)
    yield frame({}, ch["finish_reason"])
    yield "data: [DONE]\n\n"


def _call_native(native: dict) -> dict:
    r = urllib.request.urlopen(urllib.request.Request(
        OLLAMA_BASE + "/api/chat", data=json.dumps(native).encode(),
        headers={"Content-Type": "application/json"}), timeout=UPSTREAM_TIMEOUT)
    return json.loads(r.read().decode())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet by default
        pass

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _proxy_passthrough(self):
        """Forward any non-translated path to Ollama unchanged."""
        body = self._read_body()
        url = OLLAMA_BASE + self.path
        req = urllib.request.Request(url, data=body or None, method=self.command)
        for h in ("Content-Type", "Authorization", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            r = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
            data = r.read()
            self._send(r.status, data,
                       r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())
        except Exception as e:
            self._send(502, json.dumps({"error": str(e)}).encode())

    def _handle_chat(self):
        try:
            req = json.loads(self._read_body() or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "invalid JSON"}).encode())
        model = req.get("model")
        want_stream = bool(req.get("stream"))
        try:
            native = _call_native(_to_native(req))
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read())
        except Exception as e:
            return self._send(502, json.dumps(
                {"error": {"message": f"bridge upstream error: {e}"}}).encode())
        v1 = _to_v1_response(native, model)
        if not want_stream:
            return self._send(200, json.dumps(v1).encode())
        # streaming client: emit SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in _v1_to_stream_chunks(v1):
            self.wfile.write(chunk.encode())
            self.wfile.flush()

    def do_POST(self):
        if self.path.rstrip("/") == "/v1/chat/completions":
            return self._handle_chat()
        return self._proxy_passthrough()

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b'{"ok":true}')
        return self._proxy_passthrough()


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", BRIDGE_PORT), Handler)
    print(f"u1_ollama_bridge: :{BRIDGE_PORT}/v1 -> {OLLAMA_BASE}/api/chat "
          f"(native, leak-free); passthrough for everything else", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
