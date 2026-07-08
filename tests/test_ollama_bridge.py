"""Unit tests for the OpenAI-compat -> Ollama-native translation.

The bridge exists to keep gemma tool calls off Ollama's leaky /v1 serializer by
routing through native /api/chat and re-enveloping the result. These tests pin
the pure translation functions (no live model needed) so the envelope stays
correct; the live end-to-end behavior is proven separately against real Ollama.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import u1_ollama_bridge as br


def test_request_v1_to_native_maps_sampling_and_tools():
    req = {
        "model": "gemma4-26b-64k-tool:latest",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "form"}}],
        "temperature": 0.2, "top_p": 0.95, "max_tokens": 512, "stream": True,
    }
    n = br._to_native(req)
    assert n["stream"] is False              # always non-stream upstream
    assert n["tools"] == req["tools"]
    assert n["options"] == {"temperature": 0.2, "top_p": 0.95, "num_predict": 512}
    assert "think" not in n                  # no reasoning requested


def test_request_reasoning_enables_native_think():
    for key in ("reasoning_effort", "reasoning", "reasoning_config"):
        n = br._to_native({"model": "m", "messages": [], key: "medium"})
        assert n.get("think") is True


def test_native_tool_calls_become_v1_shape():
    native_tcs = [{"id": "call_x", "function": {"name": "form",
                                                "arguments": {"form_id": "abc"}}}]
    v1 = br._native_tool_calls_to_v1(native_tcs)
    assert v1[0]["type"] == "function"
    assert v1[0]["function"]["name"] == "form"
    # arguments must be a JSON STRING on /v1, not a dict
    assert v1[0]["function"]["arguments"] == '{"form_id": "abc"}'
    assert json.loads(v1[0]["function"]["arguments"]) == {"form_id": "abc"}


def test_missing_tool_call_id_is_synthesized():
    v1 = br._native_tool_calls_to_v1([{"function": {"name": "f", "arguments": {}}}])
    assert v1[0]["id"].startswith("call_")


def test_response_envelope_tool_call():
    native = {"message": {"content": "",
                          "tool_calls": [{"function": {"name": "form",
                                                       "arguments": {"form_id": "z"}}}]},
              "done_reason": "stop", "prompt_eval_count": 90, "eval_count": 70}
    v1 = br._to_v1_response(native, "gemma4-26b-64k-tool:latest")
    assert v1["object"] == "chat.completion"
    ch = v1["choices"][0]
    assert ch["finish_reason"] == "tool_calls"     # tool calls override done_reason
    assert ch["message"]["tool_calls"][0]["function"]["name"] == "form"
    assert v1["usage"] == {"prompt_tokens": 90, "completion_tokens": 70,
                           "total_tokens": 160}


def test_response_envelope_plain_text():
    native = {"message": {"content": "READY"}, "done_reason": "stop"}
    v1 = br._to_v1_response(native, "m")
    assert v1["choices"][0]["message"]["content"] == "READY"
    assert v1["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in v1["choices"][0]["message"]


def test_stream_frames_are_valid_openai_sse():
    v1 = br._to_v1_response(
        {"message": {"content": "",
                     "tool_calls": [{"function": {"name": "form",
                                                  "arguments": {"form_id": "z"}}}]},
         "done_reason": "stop"}, "m")
    frames = list(br._v1_to_stream_chunks(v1))
    assert frames[-1] == "data: [DONE]\n\n"
    first = json.loads(frames[0][6:])          # strip "data: "
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["role"] == "assistant"
    assert first["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "form"
    finish = json.loads(frames[1][6:])
    assert finish["choices"][0]["finish_reason"] == "tool_calls"


def test_no_template_tokens_ever_emitted_from_clean_native():
    """The whole point: a clean native response translates to a clean /v1
    payload with no chat-template residue anywhere."""
    native = {"message": {"content": "",
                          "tool_calls": [{"function": {"name": "form",
                                                       "arguments": {"form_id": "z"}}}]},
              "done_reason": "stop"}
    blob = json.dumps(br._to_v1_response(native, "m"))
    assert not any(t in blob for t in ("<|", "|>", "<tool_", "channel"))
