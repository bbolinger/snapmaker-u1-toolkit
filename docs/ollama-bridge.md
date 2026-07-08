# Ollama tool-call bridge (optional reliability shim)

If you drive this toolkit with a **local gemma model through Ollama**, you may
hit intermittent garbled tool calls — chat-template tokens like `<|tool_call|>`
or `<|channel|>` leaking into the reply instead of a parsed tool call. That is
[ollama/ollama#15798](https://github.com/ollama/ollama/issues/15798): Ollama's
OpenAI-compatible endpoint (`/v1/chat/completions`) sometimes mangles gemma tool
calls. The **native** endpoint (`/api/chat`) parses them cleanly, every time.

`tools/u1_ollama_bridge.py` is a tiny, zero-dependency shim that closes the gap:
it accepts the `/v1` request your agent runtime already sends, forwards it to
`/api/chat`, and returns the clean native result re-enveloped in `/v1` shape.
The broken serializer is never in the path.

## Run it

Python 3 stdlib only — no install:

```bash
# defaults: listen :11435, forward to 127.0.0.1:11434
python3 tools/u1_ollama_bridge.py

# or point it at an Ollama on another host / a different port:
OLLAMA_BASE=http://192.168.1.5:11434 BRIDGE_PORT=11435 \
  python3 tools/u1_ollama_bridge.py
```

Then point your agent runtime's OpenAI provider `base_url` at the bridge instead
of Ollama directly:

```
http://<bridge-host>:11435/v1
```

Everything except `POST /v1/chat/completions` is proxied to Ollama byte-for-byte,
so `/v1/models` and the native `/api/*` routes keep working and provider startup
is unaffected.

## Notes

- The bridge always calls Ollama **non-streaming** internally (one clean
  tool-call payload), then returns it whole or as a single well-formed SSE delta
  when the client asked to stream. It does not do incremental tool-call-delta
  reassembly — that fragility is exactly what this avoids.
- It is stateless and side-effect free. Run it as a background process, a
  container, or a service unit — whatever your setup uses. `GET /healthz`
  returns `{"ok":true}`.
- This is optional. If your model/serving stack already returns clean tool
  calls, you do not need it.
