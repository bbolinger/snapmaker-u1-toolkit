"""The model positional is a RELAY input and relays corrupt it.

Live 2026-07-06: the agent dropped the positional entirely (covered by the
original self-heal). Live 2026-07-07: the agent RETYPED the verbatim command
and duplicated four characters inside the doc-cache id, producing a path
that exists nowhere — the run died with "unsupported model file" even though
the request had persisted the real path at ingest. Both corruptions recover
from disk; only a corruption with nothing persisted still fails.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import u1_kit_workflow as kw
import u1_request


def _resolve_model_arg(model, request_id):
    """Drive just the recovery preamble the way run_kit_workflow does."""
    args = SimpleNamespace(model=model, request_id=request_id)
    model_arg = getattr(args, "model", None)
    _mangled = None
    if (model_arg and getattr(args, "request_id", None)
            and not Path(model_arg).exists()):
        _mangled = model_arg
        model_arg = None
    if not model_arg and getattr(args, "request_id", None):
        existing = u1_request.read_request(args.request_id) or {}
        recovered = existing.get("model_path")
        if recovered and Path(recovered).exists():
            return recovered, True
    return model_arg or _mangled, False


@pytest.fixture()
def seeded(tmp_path):
    # conftest already sandboxes SNAPMAKER_U1_DATA_DIR per test
    rid = "u1_2026_0707_bea101"
    real = tmp_path / "doc_ad25bc83979b_model.zip"
    real.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    u1_request.write_request(rid, model_path=str(real))
    return rid, real


def test_mangled_positional_recovers_from_request_state(seeded):
    rid, real = seeded
    mangled = str(real).replace("doc_ad25", "doc_ad25_ad25")
    resolved, healed = _resolve_model_arg(mangled, rid)
    assert healed and resolved == str(real)


def test_dropped_positional_still_recovers(seeded):
    rid, real = seeded
    resolved, healed = _resolve_model_arg(None, rid)
    assert healed and resolved == str(real)


def test_valid_positional_is_untouched(seeded):
    rid, real = seeded
    resolved, healed = _resolve_model_arg(str(real), rid)
    assert not healed and resolved == str(real)


def test_mangled_with_no_persisted_path_fails_on_original(tmp_path):
    rid = "u1_2026_0707_bea102"
    u1_request.write_request(rid, phase="whatever")  # no model_path
    resolved, healed = _resolve_model_arg("/nope/mangled.zip", rid)
    assert not healed and resolved == "/nope/mangled.zip"
