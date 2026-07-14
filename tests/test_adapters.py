"""Tests for the reference form-protocol adapters (pure cores, no SDK needed)."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_ADAPTERS = Path(__file__).resolve().parent.parent / "adapters"
sys.path.insert(0, str(_ADAPTERS / "telegram"))
sys.path.insert(0, str(_ADAPTERS / "discord"))

import u1_form  # the core, to build a real schema
import u1_form_telegram as tg
import u1_form_discord as dc


def _load_hermes_install():
    """Import adapters/hermes/install.py under a non-clashing module name."""
    path = _ADAPTERS / "hermes" / "install.py"
    spec = importlib.util.spec_from_file_location("u1_hermes_install", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hermes_install = _load_hermes_install()


def _schema(n_parts=3, n_profiles=2):
    spec = {
        "parts": [{"id": f"{i:02d}_p{i}", "label": f"p{i}"} for i in range(1, n_parts + 1)],
        "tools": ["T0", "T1", "T2", "T3"],
        "materials": ["PLA", "PETG"],
        "profiles": [{"idx": i + 1, "label": f"profile{i + 1}"} for i in range(n_profiles)],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
    }
    return u1_form.build_form_schema(spec)


def _schema_action(n_parts=3, n_profiles=2):
    """v2.2 kit forms dropped the submit_choice action field (one-decision).
    The renderer still SUPPORTS submit verbs, so these helpers keep a schema
    that has one for the verb-mechanism tests."""
    sc = _schema(n_parts, n_profiles)
    sc["fields"].append({"id": "action", "type": "single_select", "label": "Action",
                         "submit_choice": True,
                         "options": ["start", "upload-only"], "default": "start"})
    return sc


def _heads_schema_action(n_parts=2):
    sc = u1_form.build_form_schema(_heads_spec(n_parts))
    sc["fields"].append({"id": "action", "type": "single_select", "label": "Action",
                         "submit_choice": True,
                         "options": ["start", "upload-only"], "default": "start"})
    return sc


# --------------------------------------------------------------------------- #
# Telegram pure core — step-by-step state machine
# --------------------------------------------------------------------------- #

def _ids_in_keyboard(keyboard):
    return [b["callback_data"] for row in keyboard for b in row]


def _fi(schema, fid):
    return [i for i, f in enumerate(schema["fields"]) if f["id"] == fid][0]


def test_tg_new_form_starts_at_first_field():
    form = tg.new_form(_schema())
    assert form["current"] == form["schema"]["fields"][0]["id"]  # "parts" (multi)
    assert form["selections"]["parts"] == set()


def test_tg_render_field_screen_shows_field_and_step_hint():
    form = tg.new_form(_schema())
    screen = tg.render_screen(form)
    assert "Parts" in screen["text"]
    assert "Step 1 of" in screen["text"]
    # Cancel always reachable; Done present on multi screen
    cbs = _ids_in_keyboard(screen["keyboard"])
    assert "X" in cbs
    assert "n:0" in cbs    # Done for parts
    assert "a:0" in cbs    # All
    assert "z:0" in cbs    # None


def test_tg_multi_toggle_marks_in_place_and_done_advances():
    form = tg.new_form(_schema(n_parts=3))
    # toggle parts 0 and 2
    assert tg.apply_callback(form, "t:0:0")["kind"] == "rerender"
    assert tg.apply_callback(form, "t:0:2")["kind"] == "rerender"
    kb = tg.render_screen(form)["keyboard"]
    # ✔ on the toggled rows
    assert any(b["text"].startswith("✔") and b["callback_data"] == "t:0:0" for row in kb for b in row)
    assert not any(b["text"].startswith("✔") and b["callback_data"] == "t:0:1" for row in kb for b in row)
    # Next → advance to the next SCREEN (the setup group starts at 'tool')
    tg.apply_callback(form, "n:0")
    assert form["current"] == "tool"


def test_tg_single_select_in_group_does_not_advance_but_ungrouped_does():
    schema = _schema()
    form = tg.new_form(schema)
    tg.apply_callback(form, "n:%d" % _fi(schema, "parts"))   # into the setup group
    assert form["current"] == "tool"
    # a grouped single_select is a radio — tap marks, does NOT advance
    ev = tg.apply_callback(form, "s:%d:0" % _fi(schema, "tool"))
    assert ev["kind"] == "rerender"
    assert form["current"] == "tool"
    assert form["selections"]["tool"] == 0
    # an UNGROUPED single_select (profile) still advances on tap
    form["current"] = "profile"
    tg.apply_callback(form, "s:%d:0" % _fi(schema, "profile"))
    assert form["current"] == tg.REVIEW_FIELD


def test_tg_edit_from_review_returns_to_review():
    """Trap fix (live 2026-07-06): editing a field FROM the Review screen and
    then advancing must return to Review, not march the operator forward through
    the remaining screens (or dead-end). Going back to re-edit the head used to
    trap the operator with no way to confirm."""
    schema = _schema()
    form = tg.new_form(schema)
    form["current"] = tg.REVIEW_FIELD  # finished the form
    # edit a GROUPED field (tool, in the setup group)
    tg.apply_callback(form, "e:%d" % _fi(schema, "tool"))
    assert form["current"] == "tool" and form.get("_edit_return")
    # the group's shared Next must return to Review, NOT advance to 'profile'
    tg.apply_callback(form, "n:%d" % _fi(schema, "tool"))
    assert form["current"] == tg.REVIEW_FIELD
    assert not form.get("_edit_return")  # flag cleared
    # and an UNGROUPED field (profile): edit + tap also returns to Review
    tg.apply_callback(form, "e:%d" % _fi(schema, "profile"))
    tg.apply_callback(form, "s:%d:0" % _fi(schema, "profile"))
    assert form["current"] == tg.REVIEW_FIELD


def test_tg_all_and_none_shortcuts():
    form = tg.new_form(_schema(n_parts=4))
    tg.apply_callback(form, "a:0")  # All
    assert form["selections"]["parts"] == {0, 1, 2, 3}
    tg.apply_callback(form, "z:0")  # None
    assert form["selections"]["parts"] == set()


def test_tg_paginates_long_fields():
    # Profile field with 16 options should paginate
    schema = _schema(n_parts=2, n_profiles=16)
    form = tg.new_form(schema)
    pf = _fi(schema, "profile")
    # Jump cursor to profile to inspect its screen
    form["current"] = "profile"
    s = tg.render_screen(form)
    cbs = _ids_in_keyboard(s["keyboard"])
    assert any(c == f"p:{pf}:1" for c in cbs)   # next page present
    opt_cbs = [c for c in cbs if c.startswith(f"s:{pf}:")]
    assert len(opt_cbs) == tg.PAGE_SIZE
    tg.apply_callback(form, f"p:{pf}:1")
    s2 = tg.render_screen(form)
    cbs2 = _ids_in_keyboard(s2["keyboard"])
    assert any(c == f"p:{pf}:0" for c in cbs2)  # Prev present
    opt_cbs2 = [c for c in cbs2 if c.startswith(f"s:{pf}:")]
    assert len(opt_cbs2) == 16 - tg.PAGE_SIZE


def test_tg_review_card_after_last_field_lists_all_and_offers_edit():
    form = tg.new_form(_schema(n_parts=2))
    form["selections"]["parts"] = {0, 1}        # all
    form["selections"]["orient"] = 1            # auto
    form["selections"]["tool"] = 0              # T0
    form["selections"]["material"] = 0          # PLA
    form["selections"]["profile"] = 0           # profile1
    form["selections"]["supports"] = 1          # no-supports
    form["current"] = tg.REVIEW_FIELD
    s = tg.render_screen(form)
    assert "Review" in s["text"]
    assert "Auto-rotate" in s["text"] and "T0" in s["text"]
    cbs = _ids_in_keyboard(s["keyboard"])
    # v2.2: no action field -> a single plain Submit (bare "S") + Cancel.
    assert "S" in cbs and "X" in cbs
    assert not any(c.startswith("S:") for c in cbs)     # no submit verbs
    assert any(c.startswith("e:") for c in cbs)         # Edit buttons


def test_tg_submit_blocks_when_required_unset_and_jumps_back():
    schema = _schema_action()
    form = tg.new_form(schema)
    # tap a submit verb straight from review without setting tool/material/profile
    form["current"] = tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S:%d:0" % _fi(schema, "action"))
    assert ev["kind"] == "rerender"
    assert "warning" in ev and ("Tool" in ev["warning"] or "Material" in ev["warning"])
    # form cursor should land on a required-but-unset field, not stay at review
    assert form["current"] != tg.REVIEW_FIELD


def test_tg_bare_S_on_verb_schema_rerenders_to_pick_a_verb():
    # Safety: a stale/injected bare "S" must NOT submit with a silently
    # defaulted (start) action — it re-shows the review so the operator picks
    # an explicit verb.
    schema = _schema_action(n_parts=0)
    form = tg.new_form(schema)
    for f in schema["fields"]:
        if f.get("required"):
            form["selections"][f["id"]] = 0
    form["current"] = tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S")
    assert ev["kind"] == "rerender" and "Upload" in ev["warning"]
    assert form["current"] == tg.REVIEW_FIELD


def test_tg_submit_with_all_required_yields_answer_json():
    form = tg.new_form(_schema_action(n_parts=3))
    form["selections"]["parts"] = {0, 2}
    def _opt_i(fid, oid):
        f = next(f for f in form["schema"]["fields"] if f["id"] == fid)
        return next(i for i, o in enumerate(f["options"]) if tg._opt_id(o) == oid)
    form["selections"]["orient"] = _opt_i("orient", "auto")
    form["selections"]["tool"] = 0              # T0
    form["selections"]["material"] = 0          # PLA
    form["selections"]["profile"] = 1           # profile2
    form["selections"]["supports"] = _opt_i("supports", "no-supports")
    form["current"] = tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S:%d:0" % _fi(form["schema"], "action"))   # start verb
    assert ev["kind"] == "submit"
    a = ev["answer"]
    assert a["parts"] == ["01_p1", "03_p3"]
    assert a["orient"] == "auto" and a["tool"] == "T0" and a["material"] == "PLA"
    assert a["profile"] == 2 and a["supports"] == "no-supports" and a["action"] == "start"


def test_tg_edit_from_review_returns_to_that_field():
    schema = _schema()
    form = tg.new_form(schema)
    form["current"] = tg.REVIEW_FIELD
    tg.apply_callback(form, "e:%d" % _fi(schema, "tool"))
    assert form["current"] == "tool"


def test_tg_cancel_returns_cancel_event():
    form = tg.new_form(_schema())
    ev = tg.apply_callback(form, "X")
    assert ev["kind"] == "cancel"


def test_tg_answer_collapses_all_parts_to_all_keyword():
    form = tg.new_form(_schema(n_parts=2))
    form["selections"]["parts"] = {0, 1}
    assert tg.answer_json(form)["parts"] == "all"


def test_tg_full_walkthrough_feeds_parse_answers_json():
    """End-to-end: build form → tap through → answer_json → core JSON parser."""
    spec = {
        "parts": [{"id": "01_a", "label": "a"}, {"id": "02_b", "label": "b"}],
        "tools": ["T0", "T1"], "materials": ["PLA"],
        "profiles": [{"idx": 1, "label": "Std"}, {"idx": 2, "label": "Opt"}],
        "supports": ["supports", "no-supports", "overhangs"],
        "actions": ["start", "upload-only"],
    }
    schema = u1_form.build_form_schema(spec)
    form = tg.new_form(schema)

    def fi(fid):
        return _fi(schema, fid)
    # parts: tap option 0, Next
    tg.apply_callback(form, "t:%d:0" % fi("parts"))
    tg.apply_callback(form, "n:%d" % fi("parts"))
    # setup group: tool/material/orient/supports are radios (no advance)
    tg.apply_callback(form, "s:%d:0" % fi("tool"))       # T0
    tg.apply_callback(form, "s:%d:0" % fi("material"))   # PLA
    tg.apply_callback(form, "s:%d:1" % fi("orient"))     # auto
    tg.apply_callback(form, "s:%d:1" % fi("supports"))   # no-supports
    assert form["current"] == "tool"                     # still on the group
    tg.apply_callback(form, "n:%d" % fi("tool"))         # shared Next -> profile
    tg.apply_callback(form, "s:%d:1" % fi("profile"))    # Opt (advances)
    assert form["current"] == tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S")   # plain Submit (no action field now)
    assert ev["kind"] == "submit"
    r = u1_form.parse_answers_json(ev["answer"], spec)
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [1]
    assert r["values"]["tool"] == "T0"
    assert r["values"]["profile"]["idx"] == 2


def test_tg_callback_data_under_telegram_64byte_cap():
    # All callback_data must stay under Telegram's 64-byte cap.
    form = tg.new_form(_schema(n_parts=9, n_profiles=16))
    for state_field in form["schema"]["fields"]:
        form["current"] = state_field["id"]
        s = tg.render_screen(form)
        for row in s["keyboard"]:
            for b in row:
                assert len(b["callback_data"].encode("utf-8")) <= 64, b


# --------------------------------------------------------------------------- #
# Discord pure core (unchanged — native multi-select means no screen flow)
# --------------------------------------------------------------------------- #

def test_dc_components_multi_and_single():
    schema = _schema(3)
    rows = dc.build_components(schema)
    menus = {r["components"][0]["custom_id"]: r["components"][0] for r in rows}
    parts = menus["u1form:parts"]
    assert parts["type"] == 3 and parts["max_values"] == 3
    assert {o["value"] for o in parts["options"]} == {"01_p1", "02_p2", "03_p3"}
    tool = menus["u1form:tool"]
    assert tool["max_values"] == 1 and tool["min_values"] == 1


def test_dc_answer_coerces_profile_to_int_and_collapses_all():
    schema = _schema(2)
    ans = dc.answer_json(schema, {
        "parts": ["01_p1", "02_p2"], "tool": ["T0"], "material": ["PLA"], "profile": ["2"],
    })
    assert ans["parts"] == "all"
    assert ans["tool"] == "T0"
    assert ans["profile"] == 2


def test_dc_answer_feeds_parse_answers_json():
    spec = {
        "parts": [{"id": "01_p1", "label": "p1"}, {"id": "02_p2", "label": "p2"}],
        "tools": ["T0"], "materials": ["PLA"], "profiles": [{"idx": 1, "label": "Std"}],
        "supports": ["supports", "no-supports", "overhangs"], "actions": ["start", "upload-only"],
    }
    schema = u1_form.build_form_schema(spec)
    ans = dc.answer_json(schema, {"parts": ["02_p2"], "tool": ["T0"], "material": ["PLA"], "profile": ["1"]})
    r = u1_form.parse_answers_json(ans, spec)
    assert r["ok"], r["errors"]
    assert r["values"]["parts"] == [2]


# --------------------------------------------------------------------------- #
# Review fixes
# --------------------------------------------------------------------------- #

def test_tg_malformed_callback_returns_clean_warning_not_exception():
    # Review MED-1: stale callback after redeploy, or out-of-range index, must
    # not propagate an IndexError/ValueError — return a rerender with a warning.
    form = tg.new_form(_schema())
    # field index way out of range
    ev = tg.apply_callback(form, "s:99:0")
    assert ev["kind"] == "rerender" and "warning" in ev
    # option index out of range for an existing field
    ev = tg.apply_callback(form, "s:1:999")
    assert ev["kind"] == "rerender" and "warning" in ev
    # garbage shape
    ev = tg.apply_callback(form, "garbage_data")
    assert ev["kind"] == "rerender" and "warning" in ev
    # non-int field index
    ev = tg.apply_callback(form, "s:not_an_int:0")
    assert ev["kind"] == "rerender" and "warning" in ev


def test_tg_X_and_S_still_work_alongside_defensive_wrap():
    # Make sure the defensive wrap didn't break the happy paths.
    form = tg.new_form(_schema())
    assert tg.apply_callback(form, "X")["kind"] == "cancel"
    form2 = tg.new_form(_schema_action(n_parts=0))
    # set required fields then submit
    form2["selections"]["tool"] = 0
    form2["selections"]["material"] = 0
    form2["selections"]["profile"] = 0
    form2["current"] = tg.REVIEW_FIELD
    assert tg.apply_callback(form2, "S:%d:0" % _fi(form2["schema"], "action"))["kind"] == "submit"


# --------------------------------------------------------------------------- #
# HTML escaping — message text is sent with ParseMode.HTML
# --------------------------------------------------------------------------- #

def test_tg_field_screen_escapes_html_in_label():
    schema = _schema()
    schema["fields"][0]["label"] = "<b>evil</b> & friends"
    form = tg.new_form(schema)
    s = tg.render_screen(form)
    assert "<b>evil</b>" not in s["text"]
    assert "&lt;b&gt;evil&lt;/b&gt; &amp; friends" in s["text"]


def test_tg_review_card_escapes_labels_and_echo_but_not_button_text():
    schema = _schema(n_parts=2)
    schema["fields"][0]["label"] = "<i>Parts</i>"
    schema["fields"][0]["options"][0]["label"] = "bracket<v2>.stl"
    form = tg.new_form(schema)
    form["selections"]["parts"] = {0}
    form["current"] = tg.REVIEW_FIELD
    s = tg.render_screen(form)
    # Schema-derived strings in the message text are escaped entities...
    assert "&lt;i&gt;Parts&lt;/i&gt;" in s["text"]
    assert "bracket&lt;v2&gt;.stl" in s["text"]
    # ...never raw markup that Telegram would reject / render.
    assert "bracket<v2>.stl" not in s["text"]
    assert "<i>Parts</i>" not in s["text"]
    # InlineKeyboardButton text is NOT parsed as HTML — must stay raw
    # (escaping there would show literal `&lt;` to the operator).
    edit_texts = [b["text"] for row in s["keyboard"] for b in row
                  if b["callback_data"].startswith("e:")]
    assert any("<i>Parts</i>" in t for t in edit_texts)
    assert not any("&lt;" in t for t in edit_texts)


def test_tg_required_warning_escapes_field_labels():
    schema = _schema_action()
    for f in schema["fields"]:
        if f["id"] == "tool":
            f["label"] = "<b>Tool</b>"
    form = tg.new_form(schema)
    form["current"] = tg.REVIEW_FIELD
    ev = tg.apply_callback(form, "S:%d:0" % _fi(schema, "action"))
    assert ev["kind"] == "rerender"
    assert "<b>" not in ev["warning"]
    assert "&lt;b&gt;Tool&lt;/b&gt;" in ev["warning"]


# --------------------------------------------------------------------------- #
# Hermes plugin — loading, tool registration, handler bridge, adapter patch
# --------------------------------------------------------------------------- #
# The u1-form plugin is what makes the tool VISIBLE: plugin-provided
# toolsets are auto-enabled per platform by Hermes' _get_platform_tools
# (the first-party path). A tool dropped into tools/ registers but is never
# offered — built-in toolsets resolve by subset-inference against the
# platform composite, which a runtime toolset can't satisfy, and joining an
# existing toolset (clarify) evicts it. See test_hermes_real_package.py for
# the against-the-real-package proof of that invariant.

_PLUGIN_DIR = _ADAPTERS / "hermes" / "plugin"


def _load_plugin_pkg(monkeypatch, tmp_path):
    """Load the plugin exactly the way Hermes' loader does: as a package
    (``hermes_plugins.u1_form``) with the plugin dir on its search path —
    with the renderer copied in, as install.py deploys it."""
    import shutil as _sh
    pdir = tmp_path / "u1-form"
    pdir.mkdir()
    for name in ("plugin.yaml", "__init__.py", "telegram_patch.py"):
        _sh.copy(_PLUGIN_DIR / name, pdir / name)
    _sh.copy(_ADAPTERS / "telegram" / "u1_form_telegram.py",
             pdir / "u1_form_telegram.py")

    mod_name = "hermes_plugins.u1_form"
    parent = types.ModuleType("hermes_plugins")
    parent.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_plugins", parent)
    spec = importlib.util.spec_from_file_location(
        mod_name, pdir / "__init__.py",
        submodule_search_locations=[str(pdir)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = mod_name
    mod.__path__ = [str(pdir)]
    monkeypatch.setitem(sys.modules, mod_name, mod)
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    def __init__(self):
        self.tools = []
        self.hooks = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))


def test_plugin_registers_form_tool_and_dispatch_hook(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    ctx = _FakeCtx()
    mod.register(ctx)
    # The plugin always registers `form`; it ALSO registers the deterministic
    # `u1_kit` tool when tools.u1_kit_tool is importable (present in a full
    # install / on-box venv, absent in CI's isolated plugin load). Select `form`
    # explicitly rather than assuming it is the only registered tool.
    tools_by_name = {t["name"]: t for t in ctx.tools}
    assert set(tools_by_name) <= {"form", "u1_kit"}, tools_by_name
    tool = tools_by_name["form"]
    assert tool["toolset"] == "form"  # own toolset — the plugin path is what
    # surfaces it; joining clarify would evict clarify via subset-inference
    assert callable(tool["handler"])
    # Flat contract: the agent passes ONLY form_id (gemma4 couldn't emit
    # the nested schema); form_schema stays as a legacy optional property.
    assert tool["schema"]["parameters"]["required"] == ["form_id"]
    assert "form_schema" in tool["schema"]["parameters"]["properties"]
    assert [h for h, _ in ctx.hooks] == ["pre_gateway_dispatch"]


# --------------------------------------------------------------------------- #
# Grace-window CANCEL button handler must be registered from an inbound
# message, NOT only as a side effect of send_form. A reprint never sends a
# form, so before this fix its countdown CANCEL button had no callback handler
# and taps were silently dropped (live 2026-07-09: reprint cancel flashed, the
# 120s grace expired, the print started anyway).
# --------------------------------------------------------------------------- #

def _patch_ensure(monkeypatch):
    """Isolate the dispatch hook from the real (PTB-dependent) patcher: force
    ensure_patched True so we can assert the hook proactively drives per-instance
    handler registration."""
    tp = importlib.import_module("hermes_plugins.u1_form.telegram_patch")
    monkeypatch.setattr(tp, "ensure_patched", lambda cls: True)
    return tp


def test_pre_dispatch_registers_cancel_handler_every_message(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    _patch_ensure(monkeypatch)
    calls = []

    class _Adapter:
        def _u1_ensure_cb_handler(self):
            calls.append("reg")

    class _Gw:
        adapters = {"telegram": _Adapter()}

    mod._pre_gateway_dispatch(gateway=_Gw(), event=None)
    assert calls == ["reg"], (
        "pre_gateway_dispatch must register the form + grace-cancel callback "
        "handler on every inbound message (a reprint sends no form)")


def test_pre_dispatch_skips_non_telegram_adapters(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    _patch_ensure(monkeypatch)
    calls = []

    class _Adapter:
        def _u1_ensure_cb_handler(self):
            calls.append("reg")

    class _Gw:
        adapters = {"discord": _Adapter()}

    mod._pre_gateway_dispatch(gateway=_Gw(), event=None)
    assert calls == [], "only the telegram adapter carries the U1 patch"


def test_pre_dispatch_survives_registration_error(monkeypatch, tmp_path):
    """A failure registering the handler must never break message dispatch."""
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    _patch_ensure(monkeypatch)

    class _Adapter:
        def _u1_ensure_cb_handler(self):
            raise RuntimeError("app not ready")

    class _Gw:
        adapters = {"telegram": _Adapter()}

    assert mod._pre_gateway_dispatch(gateway=_Gw(), event=None) is None


def _fake_form_gateway(monkeypatch):
    """Stand-in for tools.form_gateway with the callback registry surface."""
    fg = types.ModuleType("tools.form_gateway")
    fg._cbs = {}

    def set_form_callback(session_id, cb):
        fg._cbs[session_id or "__default__"] = cb
        fg._cbs["__default__"] = cb

    def get_form_callback(session_id=""):
        return fg._cbs.get(session_id) or fg._cbs.get("__default__")

    fg.set_form_callback = set_form_callback
    fg.get_form_callback = get_form_callback
    tools_pkg = types.ModuleType("tools")
    tools_pkg.form_gateway = fg
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.form_gateway", fg)
    return fg


def _schema_min():
    return {"version": 1, "fields": [{"id": "tool", "label": "Tool",
                                      "type": "single_select",
                                      "options": [{"id": "T0"}]}]}


def test_handler_routes_to_session_callback(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    fg = _fake_form_gateway(monkeypatch)
    seen = {}

    def cb(schema):
        seen["schema"] = schema
        return {"tool": "T0"}

    fg.set_form_callback("sess-1", cb)
    out = json.loads(mod._form_handler({"form_schema": _schema_min()},
                                       session_id="sess-1"))
    assert out["user_answer"] == {"tool": "T0"}
    assert seen["schema"]["fields"][0]["id"] == "tool"


def test_handler_falls_back_to_default_callback(monkeypatch, tmp_path):
    """session_id mismatch degrades to the latest gateway turn — a live
    single-operator gateway keeps working even if ids drift."""
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    fg = _fake_form_gateway(monkeypatch)
    fg.set_form_callback("sess-A", lambda s: {"tool": "T1"})
    out = json.loads(mod._form_handler({"form_schema": _schema_min()},
                                       session_id="sess-UNSEEN"))
    assert out["user_answer"] == {"tool": "T1"}


def test_handler_without_gateway_wiring_returns_error_not_crash(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    fg = _fake_form_gateway(monkeypatch)  # registry present but EMPTY
    out = json.loads(mod._form_handler({"form_schema": _schema_min()},
                                       session_id="s"))
    assert "error" in out and "callback" in out["error"]


def test_handler_cancelled_answer_shape(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    fg = _fake_form_gateway(monkeypatch)
    fg.set_form_callback("s", lambda schema: {"_cancelled": True})
    out = json.loads(mod._form_handler({"form_schema": _schema_min()},
                                       session_id="s"))
    assert out == {"cancelled": True, "user_answer": None}


def test_handler_rejects_bad_schema_before_callback(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    _fake_form_gateway(monkeypatch)
    for bad in ({}, {"fields": []}, {"fields": "nope"}):
        out = json.loads(mod._form_handler({"form_schema": bad}, session_id="s"))
        assert "error" in out


# --------------------------------------------------------------------------- #
# pre_gateway_dispatch hook — patches the LIVE adapter class
# --------------------------------------------------------------------------- #
# The adapter source file can be imported under two module names (plugin
# loader vs namespace-package import) — two distinct class objects. The hook
# therefore patches type() of the instances in gateway.adapters: by
# construction the class the gateway dispatches through.

def _fake_adapter_cls():
    class TelegramAdapter:
        async def _handle_callback_query(self, update, ctx):
            return "original"
    return TelegramAdapter


def test_hook_patches_live_telegram_adapter_class(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    cls = _fake_adapter_cls()
    adapter = cls()

    class _P:  # stands in for the Platform enum member
        value = "telegram"

    orig_dispatcher = cls._handle_callback_query
    gateway = types.SimpleNamespace(adapters={_P(): adapter})
    mod._pre_gateway_dispatch(event=None, gateway=gateway, session_store=None)
    assert getattr(cls, "_u1_form_patched", False)
    assert hasattr(cls, "send_form")
    assert hasattr(cls, "_u1_ensure_cb_handler")
    # THE bound-method lesson (live 2026-07-02): PTB captured
    # self._handle_callback_query at connect(); replacing it on the class is
    # a silent no-op for already-registered handlers. The patch must NOT
    # swap the native dispatcher — routing goes through app.add_handler.
    assert cls._handle_callback_query is orig_dispatcher
    mod._pre_gateway_dispatch(event=None, gateway=gateway, session_store=None)
    assert cls._handle_callback_query is orig_dispatcher


def test_cb_handler_registered_on_live_app_not_class(monkeypatch, tmp_path):
    """_ensure_cb_handler registers pattern-scoped on the PTB app, group -11,
    idempotent per app object (a reconnect's fresh app re-registers)."""
    # python-telegram-bot is an OPTIONAL runtime dep — the adapter imports
    # telegram.ext lazily inside _u1_ensure_cb_handler, and this is the only
    # test that actually drives that path. Skip cleanly when it's absent (CI's
    # requirements.txt is stdlib + numpy/PIL only) instead of hard-failing.
    pytest.importorskip("telegram")
    import re
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    # Import explicitly instead of fishing sys.modules: the package loads
    # telegram_patch lazily (inside dispatch), so the sys.modules entry only
    # existed here when an earlier test happened to seed it — an ordering
    # dependency that finally bit.
    tp = importlib.import_module("hermes_plugins.u1_form.telegram_patch")
    cls = _fake_adapter_cls()

    class _P:
        value = "telegram"

    gateway = types.SimpleNamespace(adapters={_P(): cls()})
    mod._pre_gateway_dispatch(event=None, gateway=gateway, session_store=None)

    added = []

    class _FakeApp:
        # PTB's Application is __slots__-ed: arbitrary attributes raise.
        # The 2026-07-02 live failure ("no __dict__ for setting new
        # attributes") slipped through because the old fake was permissive.
        __slots__ = ()

        def add_handler(self, handler, group=0):
            added.append((handler, group))

    inst = cls()
    inst._app = _FakeApp()
    inst._u1_ensure_cb_handler()
    # Two pattern-scoped handlers per app since the grace-cancel button:
    # the form vocabulary and u1c:<request_id>.
    assert len(added) == 2 and all(g == -11 for _h, g in added)
    inst._u1_ensure_cb_handler()  # same app: no duplicates
    assert len(added) == 2
    inst._app = _FakeApp()        # reconnect: fresh app object
    inst._u1_ensure_cb_handler()
    assert len(added) == 4

    # pattern owns the ENTIRE renderer vocabulary and nothing native
    pat = re.compile(tp.FORM_CB_PATTERN)
    for ours in ("t:0:2", "s:1:3", "p:0:1", "a:0", "z:2", "n:0", "e:4", "S", "X"):
        assert pat.match(ours), ours
    for native in ("cl:1", "ea:x", "mp:2", "gt:9", "t:", "tt:1:2", "S:1", "Xx"):
        assert not pat.match(native), native


def test_hook_ignores_non_telegram_adapters(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)

    class DiscordAdapter:
        async def _handle_callback_query(self, update, ctx):
            return "original"

    class _P:
        value = "discord"

    gateway = types.SimpleNamespace(adapters={_P(): DiscordAdapter()})
    mod._pre_gateway_dispatch(event=None, gateway=gateway, session_store=None)
    assert not getattr(DiscordAdapter, "_u1_form_patched", False)
    assert not hasattr(DiscordAdapter, "send_form")


def test_hook_never_raises_and_never_blocks_dispatch(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    # gateway=None, missing adapters attr, adapter without hook point — all
    # must return None (dispatch unaffected) without raising
    assert mod._pre_gateway_dispatch(event=None, gateway=None) is None
    assert mod._pre_gateway_dispatch() is None

    class Hookless:
        pass

    class _P:
        value = "telegram"

    gateway = types.SimpleNamespace(adapters={_P(): Hookless()})
    assert mod._pre_gateway_dispatch(gateway=gateway) is None
    # hookless classes now patch fine (routing no longer needs the native
    # dispatcher); the invariant here is only: dispatch never raised.
    assert getattr(Hookless, "_u1_form_patched", False)


def test_ensure_patched_no_longer_needs_native_dispatcher(monkeypatch, tmp_path):
    """The patch no longer touches _handle_callback_query, so a class
    without it still patches fine (send_form + registered-handler routing
    are independent of the native dispatcher)."""
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    tp = sys.modules["hermes_plugins.u1_form.telegram_patch"]

    class Hookless:
        pass

    assert tp.ensure_patched(Hookless) is True
    assert hasattr(Hookless, "send_form")
    assert not hasattr(Hookless, "_handle_callback_query")
    assert tp.ensure_patched(None) is False


# --------------------------------------------------------------------------- #
# answers-file writer (gateway-side half of the v2.2 handoff)
# --------------------------------------------------------------------------- #

def _load_telegram_patch(monkeypatch, tmp_path):
    _load_plugin_pkg(monkeypatch, tmp_path)
    return sys.modules["hermes_plugins.u1_form.telegram_patch"]


def test_write_answers_file_env_dir_and_atomic(monkeypatch, tmp_path):
    tp = _load_telegram_patch(monkeypatch, tmp_path)
    dest = tmp_path / "handoff"
    monkeypatch.setenv("U1_FORM_ANSWERS_DIR", str(dest))
    path = tp.write_answers_file("abc123", {"tool": "T0"})
    assert Path(path).parent == dest
    assert json.loads(Path(path).read_text()) == {"tool": "T0"}
    assert not list(dest.glob("*.tmp.*"))  # tmp file replaced away


def test_write_answers_file_rejects_bad_form_ids(monkeypatch, tmp_path):
    tp = _load_telegram_patch(monkeypatch, tmp_path)
    monkeypatch.setenv("U1_FORM_ANSWERS_DIR", str(tmp_path / "h"))
    for bad in ("../escape", "a/b", "x", "", None, "id with spaces"):
        with pytest.raises(ValueError):
            tp.write_answers_file(bad, {})


# --------------------------------------------------------------------------- #
# form_gateway callback registry (the dispatch → gateway bridge)
# --------------------------------------------------------------------------- #

def _load_form_gateway():
    path = _ADAPTERS / "hermes" / "tools" / "form_gateway.py"
    spec = importlib.util.spec_from_file_location("u1_form_gateway_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["u1_form_gateway_test"] = mod  # @dataclass resolves cls.__module__
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("u1_form_gateway_test", None)
    return mod


def test_callback_registry_session_lookup_and_default():
    fg = _load_form_gateway()
    a, b = object(), object()
    fg.set_form_callback("sess-a", a)
    assert fg.get_form_callback("sess-a") is a
    assert fg.get_form_callback("never-seen") is a   # default = latest
    fg.set_form_callback("sess-b", b)
    assert fg.get_form_callback("sess-a") is a       # keyed slot survives
    assert fg.get_form_callback("") is b             # default follows latest


def test_callback_registry_bounds_session_slots():
    fg = _load_form_gateway()
    for i in range(fg._CB_MAX_ENTRIES + 10):
        fg.set_form_callback(f"s{i}", object())
    with fg._cb_lock:
        keyed = [k for k in fg._form_callbacks if k != "__default__"]
    assert len(keyed) <= fg._CB_MAX_ENTRIES
    assert fg.get_form_callback("anything") is not None  # default never evicted


# --------------------------------------------------------------------------- #
# Hermes install.py — copy + plugin deploy + patch + verify against a fake tree
# --------------------------------------------------------------------------- #

_STOCK_RUN_PY = (
    "def start():\n"
    "    if True:\n"
    "        if True:\n"
    "            agent.clarify_callback = _clarify_callback_sync\n"
)


def _fake_hermes(tmp_path, monkeypatch, run_py_text=_STOCK_RUN_PY):
    """Minimal Hermes venv layout install.py's discovery expects:
    <venv>/lib/pythonX.Y/site-packages/{tools/, gateway/run.py} +
    <venv>/bin/{python*, hermes}. HERMES_HOME is redirected under tmp so
    plugin deploys never touch the real home.
    """
    venv = tmp_path / "hermes-venv"
    sp = venv / "lib" / "python3.11" / "site-packages"
    (sp / "tools").mkdir(parents=True)
    (sp / "gateway").mkdir()
    run_py = sp / "gateway" / "run.py"
    run_py.write_text(run_py_text)
    bin_dir = venv / "bin"
    bin_dir.mkdir()
    # Symlink where permitted, copy where not (Windows without developer
    # mode raises WinError 1314). The installer only needs a python* file
    # to exist in bin/ — every subprocess call in these tests is stubbed.
    try:
        (bin_dir / "python3").symlink_to(sys.executable)
    except OSError:
        import shutil
        shutil.copy2(sys.executable, bin_dir / "python3")
    (bin_dir / "hermes").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    return venv, sp, run_py


def _stub_subprocess_run(calls):
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="OK: stubbed\n", stderr="")
    return fake_run


def test_install_copies_deploys_plugin_patches_and_verifies(tmp_path, monkeypatch):
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run(calls))

    rc = hermes_install.main(["--venv", str(venv)])
    assert rc == 0

    # form_gateway landed in tools/; the pre-plugin files are NOT copied.
    tools = sp / "tools"
    assert (tools / "form_gateway.py").read_bytes() == \
        (_ADAPTERS / "hermes" / "tools" / "form_gateway.py").read_bytes()
    assert not (tools / "form_tool.py").exists()
    assert not (tools / "u1_form_telegram.py").exists()

    # plugin deployed to HERMES_HOME with the renderer single-sourced from
    # adapters/telegram/ (the hermes tree keeps no copy).
    pdir = tmp_path / "hermes-home" / "plugins" / "u1-form"
    for name in ("plugin.yaml", "__init__.py", "telegram_patch.py"):
        assert (pdir / name).read_bytes() == (_PLUGIN_DIR / name).read_bytes()
    assert (pdir / "u1_form_telegram.py").read_bytes() == \
        (_ADAPTERS / "telegram" / "u1_form_telegram.py").read_bytes()
    assert not (_PLUGIN_DIR / "u1_form_telegram.py").exists()

    # run.py patched: marker present, anchor preserved, backup pristine.
    txt = run_py.read_text()
    assert hermes_install.RUN_PY_MARKER in txt
    assert hermes_install.RUN_PY_END_MARKER in txt
    assert hermes_install.RUN_PY_ANCHOR in txt
    bak = run_py.with_suffix(run_py.suffix + ".u1-bak")
    assert bak.read_text() == _STOCK_RUN_PY

    # Four subprocess steps now run, in order: enable u1-form, pip-install the
    # snapmaker_u1 hook plugin, the bare-composite toolset invariant verify, and
    # the hook-plugin registration verify.
    assert len(calls) == 4
    assert calls[0][:4] == [str(venv / "bin" / "hermes"), "plugins", "enable", "u1-form"]
    # pip install -e <repo>/plugin
    assert calls[1][0] == str(venv / "bin" / "python3")
    assert calls[1][1:5] == ["-m", "pip", "install", "-e"]
    # Path, not string suffix: Windows stringifies with backslashes.
    assert Path(calls[1][5]).name == "plugin"
    # bare-composite invariant check with syntactically valid source
    assert calls[2][0] == str(venv / "bin" / "python3")
    assert calls[2][1] == "-c"
    compile(calls[2][2], "<verify-src>", "exec")
    assert "'clarify' in ts" in calls[2][2]  # the eviction regression check
    assert "'form' in ts" in calls[2][2]
    # hook-plugin registration verify must assert transform_llm_output loads
    assert calls[3][0] == str(venv / "bin" / "python3")
    assert calls[3][1] == "-c"
    compile(calls[3][2], "<hook-verify-src>", "exec")
    assert "transform_llm_output" in calls[3][2]


def _fake_hermes_windows(tmp_path, monkeypatch, run_py_text=_STOCK_RUN_PY):
    """Native-Windows Hermes venv layout: Lib/site-packages +
    Scripts/{python.exe, hermes.exe} (install report 2026-07-10). Same
    content as _fake_hermes otherwise."""
    venv = tmp_path / "hermes-venv-win"
    sp = venv / "Lib" / "site-packages"
    (sp / "tools").mkdir(parents=True)
    (sp / "gateway").mkdir()
    run_py = sp / "gateway" / "run.py"
    run_py.write_text(run_py_text)
    scripts = venv / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_text("stub")
    (scripts / "hermes.exe").write_text("stub")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    return venv, sp, run_py


def test_install_supports_windows_venv_layout(tmp_path, monkeypatch):
    """install.py must discover Lib/site-packages and Scripts/*.exe — the
    2026-07-10 desktop report died at 'no site-packages under .../lib'."""
    venv, sp, run_py = _fake_hermes_windows(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run(calls))

    rc = hermes_install.main(["--venv", str(venv)])
    assert rc == 0

    # Same patch outcome as the POSIX layout.
    txt = run_py.read_text()
    assert hermes_install.RUN_PY_MARKER in txt
    assert (sp / "tools" / "form_gateway.py").exists()

    # Subprocess steps target the Windows executables.
    assert calls[0][0] == str(venv / "Scripts" / "hermes.exe")
    assert calls[1][0] == str(venv / "Scripts" / "python.exe")
    assert calls[1][1:5] == ["-m", "pip", "install", "-e"]


def test_install_patched_run_py_body_is_valid_python(tmp_path, monkeypatch):
    """The inserted block must compile in situ — an indent drift or template
    typo here would take down the whole gateway at import."""
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    compile(run_py.read_text(), str(run_py), "exec")
    # callback published under agent.session_id — the dispatch bridge
    assert "set_form_callback" in run_py.read_text()


def test_install_rerun_is_idempotent(tmp_path, monkeypatch):
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    once = run_py.read_text()
    assert hermes_install.main(["--venv", str(venv)]) == 0
    assert run_py.read_text() == once  # byte-identical: strip+reinsert round-trips


def test_install_upgrades_marked_block_in_place(tmp_path, monkeypatch):
    """A changed insert body (new toolkit version) replaces the old block —
    no duplicate markers, no stale body left behind."""
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    old_insert = hermes_install.RUN_PY_INSERT
    monkeypatch.setattr(hermes_install, "RUN_PY_INSERT",
                        old_insert.replace("form prompt send failed",
                                           "form prompt send FAILED"))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    txt = run_py.read_text()
    assert txt.count(hermes_install.RUN_PY_MARKER) == 1
    assert "form prompt send FAILED" in txt
    assert "form prompt send failed" not in txt
    compile(txt, str(run_py), "exec")


def test_install_removes_pre_plugin_layout_files(tmp_path, monkeypatch):
    """Upgrade path from earlier v2.2-dev deploys: the tools/ copies of
    form_tool.py / u1_form_telegram.py must be removed or the form tool
    double-registers via Hermes' tools/* auto-import."""
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    (sp / "tools" / "form_tool.py").write_text("# stale pre-plugin copy\n")
    (sp / "tools" / "u1_form_telegram.py").write_text("# stale renderer copy\n")
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    assert not (sp / "tools" / "form_tool.py").exists()
    assert not (sp / "tools" / "u1_form_telegram.py").exists()


def test_install_aborts_before_copying_when_anchor_missing(tmp_path, monkeypatch):
    """Unrecognized Hermes: the (read-only) anchor check must run BEFORE any
    file copy — otherwise Hermes auto-imports an orphaned half-install."""
    venv, sp, run_py = _fake_hermes(
        tmp_path, monkeypatch,
        run_py_text="def start():\n    pass  # layout changed upstream\n")
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    rc = hermes_install.main(["--venv", str(venv)])
    assert rc == 2
    assert list((sp / "tools").iterdir()) == []          # nothing copied
    assert not (tmp_path / "hermes-home").exists()       # no plugin deployed
    assert run_py.read_text().startswith("def start()")  # untouched
    assert not run_py.with_suffix(run_py.suffix + ".u1-bak").exists()


def test_install_refuses_malformed_marker_block(tmp_path, monkeypatch):
    """Begin marker without end marker: never edit blind."""
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    run_py.write_text(_STOCK_RUN_PY + "\n" + hermes_install.RUN_PY_MARKER + "\n")
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    rc = hermes_install.main(["--venv", str(venv)])
    assert rc == 2


def test_uninstall_restores_backup_removes_plugin_and_disables(tmp_path, monkeypatch):
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run(calls))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    assert hermes_install.main(["--venv", str(venv), "--uninstall"]) == 0
    assert run_py.read_text() == _STOCK_RUN_PY
    assert not run_py.with_suffix(run_py.suffix + ".u1-bak").exists()
    assert list((sp / "tools").iterdir()) == []
    assert not (tmp_path / "hermes-home" / "plugins" / "u1-form").exists()
    assert any(c[:3] == [str(venv / "bin" / "hermes"), "plugins", "disable"]
               for c in calls if isinstance(c, list))


def test_uninstall_after_hermes_upgrade_does_not_clobber_run_py(tmp_path, monkeypatch):
    """Upgrade scenario: pip replaced run.py (marker gone) but the old backup
    still exists. --uninstall must NOT restore it — that would downgrade
    run.py to the pre-upgrade Hermes version."""
    venv, sp, run_py = _fake_hermes(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes_install.subprocess, "run", _stub_subprocess_run([]))
    assert hermes_install.main(["--venv", str(venv)]) == 0
    upgraded = (
        "# Hermes v2 — brand new run.py from pip\n"
        "def start():\n"
        "            agent.clarify_callback = _clarify_callback_sync\n"
    )
    run_py.write_text(upgraded)  # simulate `pip install -U hermes`
    rc = hermes_install.main(["--venv", str(venv), "--uninstall"])
    assert rc == 0
    assert run_py.read_text() == upgraded  # NOT clobbered by the stale backup


# --------------------------------------------------------------------------- #
# v2.2.1 form UX — merged head/material, submit-verbs, conditional parts
# --------------------------------------------------------------------------- #

def _heads_spec(n_parts=2):
    return {
        "parts": [{"id": f"{i:02d}_p{i}", "label": f"p{i}.stl (10x10mm)"}
                  for i in range(1, n_parts + 1)],
        "heads": [
            {"tool": "T0", "channel": 0, "material": "PETG", "color": "white"},
            {"tool": "T1", "channel": 1, "material": "PETG", "color": "black"},
            {"tool": "T2", "channel": 2, "material": "PLA", "color": "orange"},
        ],
        "tool_materials": {"T0": "PETG", "T1": "PETG", "T2": "PLA"},
        "profiles": [{"idx": 1, "label": "0.20 Strength Gyroid"},
                     {"idx": 2, "label": "0.20 Standard @Snapmaker U1 (0.4 nozzle)"}],
        "supports": ["supports", "no-supports"], "actions": ["start", "upload-only"],
    }


def _fidx(schema, fid):
    return [i for i, f in enumerate(schema["fields"]) if f["id"] == fid][0]


def test_merged_head_drops_material_screen_and_labels_carry_color():
    schema = u1_form.build_form_schema(_heads_spec())
    ids = [f["id"] for f in schema["fields"]]
    assert "material" not in ids           # merged into the head
    tool_field = schema["fields"][_fidx(schema, "tool")]
    assert tool_field["label"] == "Print head"
    labels = [o["label"] for o in tool_field["options"]]
    assert labels[0] == "Head 1 (T0) — PETG ⚪ white"
    assert "🟠 orange" in labels[2]


def test_merged_head_derives_material_from_head_in_parse():
    spec = _heads_spec()
    # Pick head T2 → material must resolve to PLA without a Material answer.
    parsed = u1_form.parse_answers_json(
        {"parts": "all", "tool": "T2", "profile": 1}, spec)
    assert parsed["ok"], parsed["errors"]
    assert parsed["values"]["material"] == "PLA"


def test_profile_suffix_stripped():
    schema = u1_form.build_form_schema(_heads_spec())
    labels = [o["label"] for o in schema["fields"][_fidx(schema, "profile")]["options"]]
    assert labels == ["0.20 Strength Gyroid", "0.20 Standard"]


def test_single_part_skips_parts_screen():
    spec = _heads_spec(n_parts=1)
    schema = u1_form.build_form_schema(spec)
    assert "parts" not in [f["id"] for f in schema["fields"]]
    form = tg.new_form(schema)
    assert form["current"] == "tool"       # straight past parts, into the setup group
    # and the lone part still ends up selected by default in parse
    parsed = u1_form.parse_answers_json({"tool": "T0", "profile": 1}, spec)
    assert parsed["ok"] and parsed["values"]["parts"] == [1]


def test_submit_verb_sets_action_and_submits():
    schema = _heads_schema_action()
    form = tg.new_form(schema)
    # satisfy required screen fields
    form["selections"]["tool"] = 0
    form["selections"]["profile"] = 0
    form["current"] = tg.REVIEW_FIELD
    afi = _fidx(schema, "action")
    # tap the "upload-only" verb (option index 1)
    ev = tg.apply_callback(form, f"S:{afi}:1")
    assert ev["kind"] == "submit"
    assert ev["answer"]["action"] == "upload-only"
    # and the "start" verb
    form2 = tg.new_form(schema)
    form2["selections"]["tool"] = 1
    form2["selections"]["profile"] = 1
    form2["current"] = tg.REVIEW_FIELD
    ev2 = tg.apply_callback(form2, f"S:{afi}:0")
    assert ev2["answer"]["action"] == "start"


def test_submit_verb_still_blocks_on_missing_required():
    schema = _heads_schema_action()
    form = tg.new_form(schema)
    form["current"] = tg.REVIEW_FIELD       # nothing picked
    afi = _fidx(schema, "action")
    ev = tg.apply_callback(form, f"S:{afi}:0")
    assert ev["kind"] == "rerender" and "warning" in ev
    assert form["current"] != tg.REVIEW_FIELD


def test_offline_no_toolmap_keeps_tool_and_material_screens():
    spec = {
        "parts": [{"id": "01_a", "label": "a"}],
        "tools": ["T0", "T1"], "materials": ["PLA", "PETG"],
        "profiles": [{"idx": 1, "label": "p1"}],
        "supports": ["supports", "no-supports"], "actions": ["start", "upload-only"],
    }
    ids = [f["id"] for f in u1_form.build_form_schema(spec)["fields"]]
    assert "tool" in ids and "material" in ids   # fallback path unchanged


def test_step_counter_excludes_submit_choice():
    schema = u1_form.build_form_schema(_heads_spec())
    form = tg.new_form(schema)   # at parts
    txt = tg.render_screen(form)["text"]
    # screens: parts, setup-group (head+orient+supports), profile = 3
    assert "Step 1 of 3" in txt


# --------------------------------------------------------------------------- #
# v2.2.1 Increment 2 — grouped screen (head + orient + supports on one screen)
# --------------------------------------------------------------------------- #

def test_setup_group_renders_head_orient_supports_on_one_screen():
    schema = u1_form.build_form_schema(_heads_spec())
    # order + grouping
    assert [f["id"] for f in schema["fields"]] == \
        ["parts", "tool", "orient", "supports", "profile"]
    form = tg.new_form(schema)
    screens = [[f["id"] for f in sc] for sc in tg._screens(form)]
    assert screens == [["parts"], ["tool", "orient", "supports"], ["profile"]]
    # advance into the group
    tg.apply_callback(form, "n:%d" % _fi(schema, "parts"))
    assert form["current"] == "tool"
    s = tg.render_screen(form)
    assert "Print head &amp; layout" in s["text"]   # group title (HTML-escaped &)
    assert "orientation and supports" in s["text"].lower()   # instruction line
    # humanized, side-by-side toggle labels are on the buttons (not headers)
    btn_text = " ".join(b["text"] for row in s["keyboard"] for b in row)
    for lbl in ("Head 1 (T0)", "As-authored", "Auto-rotate", "No supports", "Add supports"):
        assert lbl in btn_text, lbl
    cbs = _ids_in_keyboard(s["keyboard"])
    assert sum(c.startswith("n:") for c in cbs) == 1   # one shared Next
    assert cbs.count("X") == 1


def test_group_radio_marks_without_advancing_shared_next_advances():
    schema = u1_form.build_form_schema(_heads_spec())
    form = tg.new_form(schema)
    tg.apply_callback(form, "n:%d" % _fi(schema, "parts"))
    for fid, oi in (("tool", 1), ("orient", 1), ("supports", 0)):
        tg.apply_callback(form, "s:%d:%d" % (_fi(schema, fid), oi))
        assert form["current"] == "tool", f"{fid} tap must not leave the group"
        assert form["selections"][fid] == oi
    # radio state renders (● on picked, ○ on others)
    kb_text = " ".join(b["text"] for row in tg.render_screen(form)["keyboard"] for b in row)
    assert "●" in kb_text and "○" in kb_text
    # shared Next advances past the whole group
    tg.apply_callback(form, "n:%d" % _fi(schema, "tool"))
    assert form["current"] == "profile"


def test_group_step_counter_counts_screens_not_fields():
    schema = u1_form.build_form_schema(_heads_spec())   # 2 parts
    form = tg.new_form(schema)
    assert "Step 1 of 3" in tg.render_screen(form)["text"]   # parts / setup / profile
    tg.apply_callback(form, "n:%d" % _fi(schema, "parts"))
    assert "Step 2 of 3" in tg.render_screen(form)["text"]   # the group is ONE step


def test_form_handler_loads_persisted_schema_by_form_id(monkeypatch, tmp_path):
    """Flat contract round-trip: workflow persists the schema, the agent
    passes only form_id, the handler loads the schema from disk and hands it
    to the gateway callback (gemma4 couldn't reproduce nested JSON in a
    tool call — Ollama #15539/#15798/#15943)."""
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    fg = _fake_form_gateway(monkeypatch)
    seen = {}
    fg.set_form_callback("sess1", lambda schema: seen.update(schema=schema) or {"ok": 1})

    sdir = tmp_path / "schemas"
    sdir.mkdir()
    monkeypatch.setenv("U1_FORM_SCHEMAS_DIR", str(sdir))
    schema = {"version": 1, "fields": [{"id": "parts", "type": "multi_select",
                                        "label": "Parts", "options": ["a", "b"]}],
              "submit": {"mode": "file", "form_id": "fabc123def"}}
    (sdir / "fabc123def.json").write_text(json.dumps(schema))

    out = json.loads(mod._form_handler({"form_id": "fabc123def"}, session_id="sess1"))
    assert out.get("user_answer") == {"ok": 1}
    assert seen["schema"]["submit"]["form_id"] == "fabc123def"


def test_form_handler_rejects_unknown_and_traversal_form_ids(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    _fake_form_gateway(monkeypatch)
    monkeypatch.setenv("U1_FORM_SCHEMAS_DIR", str(tmp_path))
    # unknown id -> structured error, no crash
    out = json.loads(mod._form_handler({"form_id": "fdeadbeef1"}, session_id="s"))
    assert "no pending form" in out.get("error", "")
    # traversal attempt -> refused by the strict pattern, never touches disk
    out2 = json.loads(mod._form_handler({"form_id": "../../etc/passwd"}, session_id="s"))
    assert "no pending form" in out2.get("error", "")


def test_form_handler_legacy_full_schema_still_works(monkeypatch, tmp_path):
    mod = _load_plugin_pkg(monkeypatch, tmp_path)
    fg = _fake_form_gateway(monkeypatch)
    fg.set_form_callback("sess2", lambda schema: {"parts": ["a"]})
    schema = {"version": 1, "fields": [{"id": "parts", "type": "multi_select",
                                        "label": "Parts", "options": ["a"]}]}
    out = json.loads(mod._form_handler(
        {"form_schema": schema}, session_id="sess2"))
    assert out.get("user_answer") == {"parts": ["a"]}



def test_form_tap_edit_strategy_v231(monkeypatch, tmp_path):
    """v2.3.1 button responsiveness: a tap that only changes the SELECTION
    (stays on the same screen) edits JUST the keyboard — snappy; a tap that
    advances to a different screen edits the full message. The old code edited
    the whole message on every tap, which Telegram throttled and made the
    selection dot lag visibly behind the tap."""
    pytest.importorskip("telegram")
    import asyncio
    _load_plugin_pkg(monkeypatch, tmp_path)
    tp = importlib.import_module("hermes_plugins.u1_form.telegram_patch")
    import u1_form as _u1form
    import u1_form_telegram as tgr

    cls = _fake_adapter_cls()
    tp.ensure_patched(cls)
    adapter = cls()
    adapter._u1_form_state = {}

    spec = {"parts": [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}],
            "tools": ["T0"], "materials": ["PLA"],
            "profiles": [{"idx": 1, "label": "x"}],
            "supports": ["supports", "no-supports"], "actions": ["start"]}
    schema = _u1form.build_form_schema(spec)
    form = tgr.new_form(schema)
    screen0 = tgr.render_screen(form)
    adapter._u1_form_state["fabc12"] = {
        "form": form, "schema": schema, "session_key": "s",
        "msg_id": 42, "chat_id": 7, "last_text": screen0["text"]}
    parts_fi = next(i for i, f in enumerate(schema["fields"]) if f["id"] == "parts")

    calls = []
    class _Msg:
        chat_id = 7
        message_id = 42
        text = screen0["text"]
    class _Q:
        def __init__(self, data):
            self.data = data
            self.message = _Msg()
        async def answer(self, *a, **k):
            pass
        async def edit_message_text(self, *a, **k):
            calls.append("text")
        async def edit_message_reply_markup(self, *a, **k):
            calls.append("markup")

    async def tap(data):
        await adapter._u1_handle_form_callback(
            types.SimpleNamespace(callback_query=_Q(data)), None)

    supports_fi = next(i for i, f in enumerate(schema["fields"])
                       if f["id"] == "supports")

    # 'Done' on parts ADVANCES to the setup group -> screen text changes ->
    # full edit_message_text.
    asyncio.run(tap("n:%d" % parts_fi))
    assert calls == ["text"], f"screen advance must be a full edit, got {calls}"

    # a grouped radio pick (supports) stays on the SAME setup screen and only
    # moves the selection dot -> keyboard-only edit (the snappy path; this is
    # the exact 'dot took forever' tap the operator hit on v2.3.0).
    calls.clear()
    asyncio.run(tap("s:%d:0" % supports_fi))
    assert calls == ["markup"], f"dot-move tap must be keyboard-only, got {calls}"
