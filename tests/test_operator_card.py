"""The print-ready message the toolkit writes itself (operator 2026-10-11:
the model's version buried the bed-clear decision above the photos).

Covers the card builder, the follow-up sender that posts the bed-clear
question after a reply's attachments, and the attachment hook that swaps the
card in for the model's text.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_SRC = Path(__file__).resolve().parent.parent / "plugin" / "src"
sys.path.insert(0, str(_PLUGIN_SRC))

from snapmaker_u1.hooks import attachment_injector as ai  # noqa: E402
from snapmaker_u1.hooks import followup  # noqa: E402
from snapmaker_u1.hooks import operator_card as oc  # noqa: E402
import u1_kit_workflow as wf  # noqa: E402

_RID = "u1_2026_1011_d12ac6"
_READY = {
    "stage": "kit_readiness_card", "request_id": _RID,
    "title": "Magnet pocket test coupon", "tool": "T0", "material": "PETG",
    "profile": "0_20_standard_snapmaker_u1_0_4_nozzle",
    "profile_label": "0.20 Standard", "supports": "no_supports",
    "orient": "as-authored", "part_count": 1, "selected_parts": ["p1"],
    "plate_count": 1,
    "estimates": {"total_time_str": "25m", "total_grams": 6.2},
}
_NEED = {"stage": "need_input", "request_id": _RID, "need": "bed_clear_start",
         "key": "bed_clear_start", "bed_snapshot_path": "/x/bed_snapshot.jpg",
         "prompt": "Sliced plate, review doc, and a fresh bed photo are attached. "
                   "Bed clear and ready to print? Reply YES to start now, or NO "
                   "to keep the gcode uploaded without printing."}


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #

def test_single_plate_card_is_short_and_the_question_stands_alone():
    card, question = oc.compose(_READY, _NEED)
    assert card == ("\U0001F5A8️ **Ready to print: Magnet pocket test coupon**\n"
                    "T0 · PETG · 0.20 Standard · no supports\n"
                    "⏱ 25 min · 6 g filament")
    assert question == ("\U0001F449 **Bed clear?** Reply **YES** to print.\n"
                        "NO keeps it on the printer, unprinted.")
    # what the old message carried that the operator never needed
    assert _RID not in card and "as-authored" not in card


def test_card_flags_what_needs_attention():
    ready = dict(_READY, part_count=8, selected_parts=["a", "b", "c"], plate_count=3,
                 orient="auto", estimates={"total_time_str": "3h 12m", "total_grams": 84.4},
                 overhang_buckets={"recommended_indices": [2, 5]})
    card, _ = oc.compose(ready, dict(_NEED, bed_snapshot_path=None))
    lines = card.splitlines()
    assert lines[1].endswith("no supports · auto-oriented")
    assert "3 of 8 parts" in lines
    assert "⏱ 3h 12m · 84 g filament" in lines
    assert any("3 plates. Plate 1 starts on YES; plates 2–3" in ln for ln in lines)
    assert any("Supports are off, but parts 2, 5 may need them." in ln for ln in lines)
    assert any("No bed photo this time" in ln for ln in lines)


def test_card_skips_missing_fields_instead_of_guessing():
    card, _ = oc.compose({"stage": "kit_readiness_card"}, {"bed_snapshot_path": "/x.jpg"})
    assert card == "\U0001F5A8️ **Ready to print**"


def test_profile_slug_is_humanized_when_no_label():
    ready = dict(_READY, profile_label=None, profile="0_16_optimal_snapmaker_u1_0_4_nozzle")
    assert "0.16 Optimal" in oc.compose(ready, _NEED)[0]


def test_supports_recommended_but_on_is_not_flagged():
    ready = dict(_READY, supports="supports", overhang_buckets={"recommended_indices": [1]})
    assert "may need them" not in oc.compose(ready, _NEED)[0]


# --------------------------------------------------------------------------- #
# The follow-up sender
# --------------------------------------------------------------------------- #

@pytest.fixture
def clean_followup(monkeypatch):
    monkeypatch.setattr(followup, "_pending", {})
    monkeypatch.setattr(followup, "_ready_platforms", set())


def test_queue_is_per_chat_and_drops_stale_entries(clean_followup, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(followup.time, "time", lambda: now[0])
    followup.queue("telegram", 42, "old")
    now[0] += followup.TTL_SECONDS + 1
    followup.queue(types.SimpleNamespace(value="Telegram"), "42", "new")
    followup.queue("telegram", 7, "other chat")
    assert followup.pop("telegram", "42") == ["new"]
    assert followup.pop("telegram", "42") == []
    assert followup.pop("telegram", 7) == ["other chat"]


class _FakeAdapter:
    def __init__(self, log):
        self.log = log

    async def on_processing_complete(self, event, outcome):
        self.log.append(("complete", outcome))

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.log.append(("send", chat_id, content, metadata))


def _event(chat_id="42", platform="telegram"):
    return types.SimpleNamespace(source=types.SimpleNamespace(
        chat_id=chat_id, platform=types.SimpleNamespace(value=platform), thread_id=None))


def test_question_goes_out_after_the_reply_finishes(clean_followup):
    cls = type("TelegramAdapter", (_FakeAdapter,), {})
    followup.install(types.SimpleNamespace(adapters={"telegram": cls([])}))
    assert followup.ready("telegram")
    log = []
    adapter = cls(log)
    followup.queue("telegram", "42", "\U0001F449 **Bed clear?**")
    asyncio.run(adapter.on_processing_complete(_event(), "SUCCESS"))
    assert log[0] == ("complete", "SUCCESS")
    assert log[1][:3] == ("send", "42", "\U0001F449 **Bed clear?**")
    assert log[1][3]["notify"] is True
    # nothing queued -> nothing sent; patching twice does not double-wrap
    followup.install(types.SimpleNamespace(adapters={"telegram": cls([])}))
    asyncio.run(adapter.on_processing_complete(_event(), "SUCCESS"))
    assert [e[0] for e in log] == ["complete", "send", "complete"]


def test_inbound_hook_installs_the_follow_up_sender(clean_followup):
    from snapmaker_u1.hooks import attachment_router as ar
    cls = type("TelegramAdapter", (_FakeAdapter,), {})
    handler = ar.make_handler("3d-printer-slicing-automation")
    assert not followup.ready("telegram")
    handler(event=None, gateway=types.SimpleNamespace(adapters={"telegram": cls([])}))
    assert followup.ready("telegram")
    assert getattr(cls, "_u1_followup_patched", False)


def test_question_still_sent_when_the_original_step_raises(clean_followup):
    class Boom(_FakeAdapter):
        async def on_processing_complete(self, event, outcome):
            raise RuntimeError("reaction failed")
    followup.install(types.SimpleNamespace(adapters={"telegram": Boom([])}))
    log = []
    followup.queue("telegram", "42", "q")
    with pytest.raises(RuntimeError):
        asyncio.run(Boom(log).on_processing_complete(_event(), "SUCCESS"))
    assert log and log[0][:3] == ("send", "42", "q")


def test_adapter_without_the_lifecycle_step_is_not_ready(clean_followup):
    class Plain:
        pass
    followup.install(types.SimpleNamespace(adapters={"irc": Plain()}))
    assert not followup.ready("irc")


# --------------------------------------------------------------------------- #
# The attachment hook swaps in the card
# --------------------------------------------------------------------------- #

@pytest.fixture
def hook(tmp_path, monkeypatch, clean_followup):
    d = tmp_path / "pending_attach"
    d.mkdir()
    (tmp_path / "requests").mkdir()
    monkeypatch.setattr(ai, "_PENDING_ATTACH_DIR", d)
    monkeypatch.setattr(wf, "_PENDING_ATTACH_DIR", d)
    monkeypatch.setattr(ai, "_REQUESTS_ROOT", tmp_path / "requests")
    monkeypatch.setenv("HERMES_SESSION_KEY", "agent:main:telegram:dm:42")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "42")
    return tmp_path


def _artifact(root, name):
    d = root / "requests" / _RID
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return str(p)


def _output(root):
    preview = _artifact(root, "plate_1_preview.png")
    review = _artifact(root, "review.md")
    events = [{"stage": "render", "request_id": _RID, "image": preview},
              {"stage": "review_doc", "request_id": _RID, "path": review},
              _READY, _NEED]
    return "\n".join(json.dumps(e) for e in events), [preview, review]


_MODEL_REPLY = ("On it, running the Snapmaker U1 workflow. I'll surface every choice "
                "for you.\n\nReady: 1 plate · T0 PETG\n\nRequest u1_2026_1011_d12ac6\n\n"
                "Bed clear and ready to print? Reply YES to start now.")


def test_print_ready_reply_is_the_card_and_the_question_follows(hook):
    out, paths = _output(hook)
    followup._ready_platforms.add("telegram")
    assert ai.arm_from_tool_result(out) == 2
    reply = ai.transform(response_text=_MODEL_REPLY, session_id="s", platform="telegram")
    card, question = oc.compose(_READY, _NEED)
    assert reply == card + "\n\n" + "\n".join(paths)
    assert "On it" not in reply and "Bed clear" not in reply
    assert followup.pop("telegram", "42") == [question]


def test_question_stays_in_the_card_when_no_follow_up_can_be_sent(hook):
    out, paths = _output(hook)
    ai.arm_from_tool_result(out)  # follow-up sender not installed
    reply = ai.transform(response_text=_MODEL_REPLY, session_id="s", platform="telegram")
    card, question = oc.compose(_READY, _NEED)
    assert reply == card + "\n\n" + question + "\n\n" + "\n".join(paths)
    assert followup.pop("telegram", "42") == []


def test_turn_without_readiness_keeps_the_models_text(hook):
    preview = _artifact(hook, "plate_1_preview.png")
    out = "\n".join(json.dumps(e) for e in (
        {"stage": "render", "request_id": _RID, "image": preview}, _NEED))
    ai.arm_from_tool_result(out)
    reply = ai.transform(response_text="Bed clear? Reply YES.", session_id="s")
    assert reply.startswith("Bed clear? Reply YES.") and preview in reply


def test_forged_card_cannot_smuggle_a_file(hook):
    preview = _artifact(hook, "plate_1_preview.png")
    marker = {"request_id": _RID, "images": [preview], "documents": [],
              "created_at": __import__("time").time(),
              "card_text": "Ready MEDIA:/etc/passwd\n/etc/hosts\n`/opt/data/.env` done",
              "question_text": "YES? /etc/shadow"}
    digest = __import__("hashlib").sha256(b"agent:main:telegram:dm:42").hexdigest()[:16]
    (ai._PENDING_ATTACH_DIR / f"{digest}.json").write_text(json.dumps(marker))
    reply = ai.transform(response_text="x", session_id="s")
    for bad in ("/etc/passwd", "/etc/hosts", "/opt/data/.env", "/etc/shadow", "MEDIA:"):
        assert bad not in reply
    assert reply.startswith("Ready\n\ndone") or "done" in reply


# --------------------------------------------------------------------------- #
# The workflow's new card fields
# --------------------------------------------------------------------------- #

def test_profile_label_comes_from_the_catalog(monkeypatch):
    monkeypatch.setattr(wf, "list_profiles", lambda nozzle=None: [
        {"value": "0_20_standard_snapmaker_u1_0_4_nozzle", "label": "0.20 Standard"}])
    assert wf._profile_label_for("0_20_standard_snapmaker_u1_0_4_nozzle", "0.4") == "0.20 Standard"
    assert wf._profile_label_for("unknown", "0.4") is None
    assert wf._profile_label_for(None, "0.4") is None

    def boom(nozzle=None):
        raise OSError("catalog unreadable")
    monkeypatch.setattr(wf, "list_profiles", boom)
    assert wf._profile_label_for("x", "0.4") is None


def test_title_drops_the_chat_cache_prefix():
    assert wf._strip_doc_prefix("doc_f0e7f2d14308_Magnet pocket test coupon") == \
        "Magnet pocket test coupon"


# --------------------------------------------------------------------------- #
# The tap-form path: the u1_kit tool's result arms the card too
# --------------------------------------------------------------------------- #

def test_u1_kit_tool_result_arms_the_card(hook):
    """Live 2026-10-11: the tool-result hook only read the terminal tool, so on
    the u1_kit (tap form) path nothing armed and the model's own text went out."""
    from snapmaker_u1.hooks import next_action_directive as nad
    out, paths = _output(hook)
    summary = json.dumps({"phase": "ready", "request_id": _RID, "readiness_card": _READY})
    followup._ready_platforms.add("telegram")
    assert nad.transform(tool_name="u1_kit", args={}, result=out + "\n" + summary) is None
    reply = ai.transform(response_text=_MODEL_REPLY, session_id="s", platform="telegram")
    card, question = oc.compose(_READY, _NEED)
    assert reply == card + "\n\n" + "\n".join(paths)
    assert followup.pop("telegram", "42") == [question]


def test_other_tools_still_do_not_arm(hook):
    from snapmaker_u1.hooks import next_action_directive as nad
    out, _ = _output(hook)
    assert nad.transform(tool_name="web_search", args={}, result=out) is None
    assert ai.transform(response_text="hello", session_id="s") is None
