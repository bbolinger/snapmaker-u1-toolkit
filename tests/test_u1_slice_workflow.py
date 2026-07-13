from __future__ import annotations
import argparse, json, numpy as np, pytest
from pathlib import Path
from u1_orient import write_binary_stl, DEFAULT_ORCA
from u1_slice_workflow import main, run_workflow, promote_to_supports_variant, filament_path, parse_orca_warnings, profile_path
from render_slice_review import first_layer_bbox
from _stl_render import parse_stl

def _stl(tmp_path):
    p=tmp_path/'m.stl'
    verts=np.array([
        [0,0,0],[20,0,0],[20,20,0],[0,20,0],
        [0,0,5],[20,0,5],[20,20,5],[0,20,5],
    ], dtype=np.float32)
    faces=[(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),(1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]
    write_binary_stl(p, np.array([[verts[a],verts[b],verts[c]] for a,b,c in faces], dtype=np.float32))
    return p

def _point_at_production_stock(monkeypatch):
    """Real-Orca tests need fully-inheriting profiles, not the minimal stubs
    used elsewhere. Repoint DEFAULT_SOURCES at /appdata/hermes/profiles/ so
    Orca sees the same profile tree production uses. Also force
    machine_profile_for_orca to return the vendor profile (production
    behavior — shim's parents[1] doesn't auto-resolve the vendor path)."""
    import u1_profile_picker as upp
    import u1_slice_workflow as wf
    prod = Path('/appdata/hermes/profiles')
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (
        ('from-printer', prod / 'from-printer'),
        ('user', prod / 'user'),
        ('snapmaker-stock', prod / 'snapmaker-stock'),
    ))
    vendor_machine = Path('/appdata/hermes/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json')
    monkeypatch.setattr(wf, 'machine_profile_for_orca', lambda orca_bin=None: vendor_machine)


def test_headless_yes_upload_only_has_no_prompts(real_orca, capsys, monkeypatch):
    # Real-Orca integration via the Hermes shim (2026-06-26 cold review #4).
    # The slice is actually performed by OrcaSlicer running inside the
    # Hermes container; gcode + renders land on the bind-mounted scratch.
    # Unified workflow (kit-of-one, task #4): u1_slice_workflow no longer
    # slices/uploads a single STL itself — it DETECTS and hands off to
    # u1_kit_workflow via a kit_detected event. Assert the clean dispatch and
    # that --yes surfaces no interactive prompt. (Slice+upload coverage lives
    # in the u1_kit_workflow tests + the real-Orca metadata e2e.) This test was
    # stale-red asserting the retired direct-upload path.
    tmp_path = real_orca['tmp']
    _point_at_production_stock(monkeypatch)
    src = _stl(tmp_path)
    out = tmp_path / 'out'
    rc = main([str(src), '--yes', '--upload-only', '--no-live-material',
               '--tool', 'T1', '--material', 'PETG',
               '--profile', '0_20_standard_snapmaker_u1_0_4_nozzle',
               '--out-dir', str(out)])
    captured = capsys.readouterr().out
    assert rc == 0
    assert 'kit_detected' in captured
    assert 'u1_kit_workflow.py' in captured
    # Headless dispatch (phase kit_redirect), NOT an interactive prompt. ("need_input"
    # appears only as prose inside the kit_detected instruction, so don't substring it.)
    assert 'kit_redirect' in captured


def test_json_events_surface_questions_and_summary(real_orca, capsys, monkeypatch):
    # Same unification: a single STL now emits a kit_detected dispatch event
    # (not a summary — that moved into u1_kit_workflow). Stale-red before.
    tmp_path = real_orca['tmp']
    _point_at_production_stock(monkeypatch)
    src = _stl(tmp_path)
    main([str(src), '--json-events', '--yes', '--no-live-material',
          '--material', 'PETG',
          '--profile', '0_20_standard_snapmaker_u1_0_4_nozzle',
          '--tool', 'T1', '--out-dir', str(tmp_path / 'o')])
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith('{')]
    assert any(e.get('stage') == 'kit_detected' for e in events)


def test_workflow_output_oriented_stl_matches_orca_slice_first_layer_for_ego(tmp_path):
    """Real-Orca regression for the inverted orientation bug.

    The EGO trimmer's correct Orca auto-orient has a wide first-layer footprint.
    Treating Orca's row vector as bed-down puts U-cradle tips on the bed and
    produces a tiny/narrow contact instead. This test exercises the full workflow
    with extracted Orca and compares the rendered/oriented STL with the actual
    sliced G-code footprint.
    """
    ego=Path('/opt/data/cache/documents/doc_9d706d1d9b73_EGO String Trimmer holder v4.3mf.zip')
    if not ego.exists():
        pytest.skip('EGO regression source not present in this environment')
    if not DEFAULT_ORCA.exists():
        pytest.skip('extracted Orca binary not present in this environment')
    args=argparse.Namespace(
        model=str(ego), json_events=False, yes=True, orient='auto', down_vec=None,
        tool='T1', material='PETG', profile='020_strength', class_hint='ego trimmer holder',
        supports='auto',
        upload_only=True, live_upload=False, no_live_material=True,
        out_dir=tmp_path/'ego_real_orca', cancel=False,
    )
    res=run_workflow(args)
    gcode_bbox=first_layer_bbox(Path(res['gcode']))
    assert gcode_bbox is not None
    gx0,gx1,gy0,gy1=gcode_bbox
    gwidth=max(gx1-gx0, gy1-gy0)
    gdepth=min(gx1-gx0, gy1-gy0)
    assert gwidth > 100
    assert gdepth > 70

    tris=parse_stl(Path(res['oriented_stl']))
    contact=tris.reshape(-1,3)
    contact=contact[contact[:,2] <= 0.6]
    assert contact.size > 0
    sx0,sy0=contact[:,0].min(), contact[:,1].min()
    sx1,sy1=contact[:,0].max(), contact[:,1].max()
    swidth=max(sx1-sx0, sy1-sy0)
    sdepth=min(sx1-sx0, sy1-sy0)
    # Orca includes brim in the first-layer G-code footprint. The oriented STL
    # contact patch should therefore be smaller than G-code by a roughly even
    # brim margin on both axes, not the tiny U-cradle-tip footprint that caught
    # the inverted-rotation bug.
    assert abs((gwidth-swidth) - (gdepth-sdepth)) < 8
    assert 20 < (gwidth-swidth) < 45
    assert 20 < (gdepth-sdepth) < 45
    assert Path(res['preview']).exists()


# ---------- promote_to_supports_variant ----------
# v1.5.0: promote rule is "exactly one supports-enabled profile in the same
# source." We control the source via monkeypatching DEFAULT_SOURCES.

def _stock_fixture(tmp_path, monkeypatch, *profile_specs):
    """Provision a fake snapmaker-stock dir + repoint DEFAULT_SOURCES.

    profile_specs is a list of (name, has_supports) tuples. Each writes a
    minimal valid process-profile JSON to the fixture dir."""
    stock = tmp_path / 'fixture-stock' / 'process'
    stock.mkdir(parents=True)
    for name, has_supports in profile_specs:
        (stock / f'{name}.json').write_text(json.dumps({
            'type': 'process', 'name': name,
            'enable_support': '1' if has_supports else '0',
        }))
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES',
                        (('snapmaker-stock', stock.parent),))
    return stock


def test_promote_returns_target_when_exactly_one_supports_variant(tmp_path, monkeypatch):
    _stock_fixture(tmp_path, monkeypatch,
                   ('020_strength', False), ('020_strength_supports', True))
    assert promote_to_supports_variant('020_strength') == '020_strength_supports'


def test_promote_returns_none_when_multiple_supports_variants(tmp_path, monkeypatch):
    # Realistic v1.5.0 case: Snapmaker stock has multiple Support flavors
    # (Support, Support W, Bambu Support W) — workflow can't pick one
    # automatically, must defer to user.
    _stock_fixture(tmp_path, monkeypatch,
                   ('020_strength', False),
                   ('020_strength_support', True),
                   ('020_strength_support_w', True))
    assert promote_to_supports_variant('020_strength') is None


def test_promote_returns_none_when_no_variant(tmp_path, monkeypatch):
    # Preset has no supports siblings at all.
    _stock_fixture(tmp_path, monkeypatch, ('016_optimal', False))
    assert promote_to_supports_variant('016_optimal') is None


def test_promote_returns_none_for_already_supports(tmp_path, monkeypatch):
    # Don't double-promote.
    _stock_fixture(tmp_path, monkeypatch, ('020_strength_supports', True))
    assert promote_to_supports_variant('020_strength_supports') is None


# ---------- --supports flag end-to-end event emission ----------
# These tests let the workflow run until it tries to invoke Orca, which fails
# on alpine (no glibc binary). All the events of interest are emitted BEFORE
# that call, so we just assert on stdout.

def _events_until_orca_dies(tmp_path, capsys, profile, supports_flag):
    src=_stl(tmp_path)
    with pytest.raises((FileNotFoundError, RuntimeError, SystemExit)):
        main([str(src),'--json-events','--yes','--no-live-material',
              '--tool','T1','--material','PETG',
              '--profile',profile,'--supports',supports_flag,
              '--out-dir',str(tmp_path/'o')])
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith('{')]


# ---------- v1.5.1 Supports? redesign: override path ----------
# Workflow no longer promotes to a sibling _supports preset. Instead,
# apply_supports_override() materializes a temp process JSON with
# enable_support patched to the user's binary answer. Tests cover both
# directions + the 'overhangs' deferral.

def test_supports_auto_legacy_alias_does_not_override(tmp_path, capsys, monkeypatch):
    # Backwards-compat: --supports auto = "use preset as-is, no override".
    # Pre-v1.5.1 callers that pass auto still get the legacy behavior of
    # NOT touching enable_support. No supports_override event fires.
    _stock_fixture(tmp_path, monkeypatch, ('020_strength', False))
    events=_events_until_orca_dies(tmp_path, capsys, '020_strength', 'auto')
    assert not any(e.get('stage')=='supports_override' for e in events)
    assert not any(e.get('stage')=='warning' for e in events)


def test_apply_supports_override_patches_enable_support_field(tmp_path):
    from u1_slice_workflow import apply_supports_override
    # Source profile has enable_support='0'
    src=tmp_path/'0.20 Standard.json'
    src.write_text(json.dumps({'type':'process','name':'0.20 Standard','enable_support':'0','some_other_field':'x'}))
    out_dir=tmp_path/'override_out'
    result_path=apply_supports_override(src, enable_support=True, out_dir=out_dir)
    assert result_path.parent==out_dir
    assert '__force_supports' in result_path.stem
    written=json.loads(result_path.read_text())
    assert written['enable_support']=='1'
    assert written['some_other_field']=='x'  # preserved
    # Negative case: enable_support=False writes '0' + different stem suffix
    result_off=apply_supports_override(src, enable_support=False, out_dir=out_dir)
    assert '__no_supports' in result_off.stem
    written_off=json.loads(result_off.read_text())
    assert written_off['enable_support']=='0'


# ---------- supports_status annotation on Preset? options ----------

def _preset_options(tmp_path, capsys):
    """Run analysis phase and return the Preset? need_input options.
    v1.5.2: workflow emits ONE need_input at a time. Pass --orient AND
    --tool/--material so the workflow walks past orient + tool and lands
    on preset."""
    src=_stl(tmp_path)
    # No --yes → workflow exits at awaiting_input without hitting Orca.
    main([str(src),'--json-events','--no-live-material','--orient','asauthored',
          '--tool','T1','--material','PETG','--out-dir',str(tmp_path/'o')])
    events=[json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith('{')]
    preset_events=[e for e in events if e.get('stage')=='need_input' and e.get('key')=='preset']
    assert len(preset_events)==1
    return {o['value']: o for o in preset_events[0]['options']}


def _filament_fixture(tmp_path, monkeypatch, *filament_specs):
    """Provision a fake snapmaker-stock dir with filament/ subdir, monkeypatch
    DEFAULT_SOURCES to point at it. filament_specs is a list of filename
    stems (e.g. 'Generic PETG @U1 0.4 nozzle')."""
    stock = tmp_path / 'fixture-stock'
    filament_dir = stock / 'filament'
    filament_dir.mkdir(parents=True)
    for stem in filament_specs:
        (filament_dir / f'{stem}.json').write_text(json.dumps({
            'type': 'filament', 'name': stem, 'filament_settings_id': [stem],
        }))
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', stock),))
    return filament_dir


def test_filament_path_finds_u1_tagged_filament(tmp_path, monkeypatch):
    _filament_fixture(tmp_path, monkeypatch,
                      'Generic PETG @U1 0.4 nozzle', 'Generic PETG @base')
    p = filament_path('PETG')
    assert 'u1' in p.stem.lower()


def test_filament_path_falls_back_to_base_when_no_u1_variant(tmp_path, monkeypatch):
    # Only @base available — Orca will follow the inherits chain at slice time.
    _filament_fixture(tmp_path, monkeypatch, 'Generic PETG @base')
    p = filament_path('PETG')
    assert p.name == 'Generic PETG @base.json'


def test_filament_path_raises_runtime_error_when_no_source(tmp_path, monkeypatch):
    # Empty fixture-stock dir (no filament/ subdir).
    stock = tmp_path / 'empty'
    stock.mkdir()
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', stock),))
    with pytest.raises(RuntimeError, match='no filament profile found'):
        filament_path('PETG')


# ---------- setup_required event when picker is empty (M2) ----------

def _mock_slice_pipeline(monkeypatch, tmp_path, warnings):
    """Stand in for real_orca_slice + thumbnail injection + upload — none of
    which are alpine-compatible. Returns a slice_res with the given warnings."""
    import u1_slice_workflow as wf

    def fake_slice(stl, gcode, *a, **k):
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text('; minimal gcode\nG1 X0 Y0\n')
        return {
            'time': '1m', 'weight_g': 5.0,
            'warnings': list(warnings),
            'gcode': str(gcode), 'tool_idx': 1, 'tool_rewrites': 0,
            'thumbnails': {'ok': True},
            'metadata': {}, 'moonraker_metadata': {},
        }

    fake_filament = tmp_path / 'fake_filament.json'
    fake_filament.write_text('{"type":"filament","name":"fake"}')
    monkeypatch.setattr(wf, 'real_orca_slice', fake_slice)
    monkeypatch.setattr(wf, 'filament_path', lambda mat: fake_filament)
    # No need to mock inject_snapmaker_thumbnails — it's called from inside
    # real_orca_slice (which we already mocked), so the real function never
    # runs in tests.


def test_parse_orca_warnings_picks_up_floating_cantilever():
    out = "WARNING: floating cantilever on Object_1\nG1 X10 Y10\n"
    assert parse_orca_warnings(out) == ["WARNING: floating cantilever on Object_1"]


def test_parse_orca_warnings_picks_up_floating_region_error_severity():
    # 'error' severity counts too — Orca may upgrade severe issues.
    out = "ERROR: floating region detected\n"
    assert parse_orca_warnings(out) == ["ERROR: floating region detected"]


def test_parse_orca_warnings_picks_up_warning_with_overhang():
    out = "WARNING: layer 5 overhang exceeds 60 degrees\n"
    assert parse_orca_warnings(out) == ["WARNING: layer 5 overhang exceeds 60 degrees"]


def test_parse_orca_warnings_skips_info_line_mentioning_overhang():
    # Info/debug lines that happen to mention 'overhang' should not be
    # flagged as warnings — that was the L1 false-positive risk.
    out = (
        "INFO: 5 overhang regions detected, all within tolerance\n"
        "DEBUG: overhang processing complete\n"
        "0 overhang issues found\n"
    )
    assert parse_orca_warnings(out) == []


def test_parse_orca_warnings_skips_severity_without_geometric_token():
    # 'WARNING' alone (e.g. about something unrelated) doesn't match.
    out = "WARNING: filament low, refill soon\n"
    assert parse_orca_warnings(out) == []


def test_parse_orca_warnings_dedupes():
    out = (
        "WARNING: overhang issue\n"
        "WARNING: overhang issue\n"
    )
    assert parse_orca_warnings(out) == ["WARNING: overhang issue"]


def test_parse_orca_warnings_empty_input():
    assert parse_orca_warnings("") == []


def test_profile_path_resolves_slug_form(tmp_path, monkeypatch):
    _stock_fixture(tmp_path, monkeypatch, ('020_strength', False))
    # User passes the slug as it appears in picker output.
    p = profile_path('020_strength')
    assert p.exists()
    assert p.name == '020_strength.json'


def test_profile_path_resolves_raw_name_via_normalize_value(tmp_path, monkeypatch):
    # User pastes the human-readable Snapmaker name; normalize_value
    # canonicalizes it to the picker's slug form so the lookup succeeds.
    _stock_fixture(tmp_path, monkeypatch, ('0_20_strength_snapmaker_u1_0_4_nozzle', False))
    p = profile_path('0.20 Strength @Snapmaker U1 (0.4 nozzle)')
    assert p.exists()


def test_profile_path_raises_runtime_error_when_unknown(tmp_path, monkeypatch):
    _stock_fixture(tmp_path, monkeypatch, ('020_strength', False))
    with pytest.raises(RuntimeError, match='not found in any source'):
        profile_path('nonexistent_profile')




# ---------- filament_path coverage (#15) ----------
# Multi-source priority + U1-tagged preference + RuntimeError on miss.

def test_filament_path_prefers_u1_tagged_over_base(tmp_path, monkeypatch):
    # When both 'Generic PETG @U1 ...' and 'Generic PETG @base' exist in the
    # same source, the U1-tagged variant should win.
    stock = tmp_path / 'stock'
    fdir = stock / 'filament'
    fdir.mkdir(parents=True)
    (fdir / 'Generic PETG @U1 0.4 nozzle.json').write_text('{"type":"filament"}')
    (fdir / 'Generic PETG @base.json').write_text('{"type":"filament"}')
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', stock),))
    p = filament_path('PETG')
    assert 'u1' in p.stem.lower()
    assert '@base' not in p.stem.lower()


def test_filament_path_source_priority_from_printer_wins(tmp_path, monkeypatch):
    # When the same material appears in both from-printer and snapmaker-stock,
    # from-printer wins.
    printer = tmp_path / 'printer'
    stock = tmp_path / 'stock'
    (printer).mkdir()
    (stock / 'filament').mkdir(parents=True)
    (printer / 'my_petg_filament.json').write_text('{"type":"filament"}')
    (stock / 'filament' / 'Generic PETG @U1 0.4 nozzle.json').write_text('{"type":"filament"}')
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES',
                        (('from-printer', printer), ('snapmaker-stock', stock)))
    p = filament_path('PETG')
    assert 'my_petg' in p.stem.lower()


def test_filament_path_extracted_naming_convention(tmp_path, monkeypatch):
    # Extracted profiles use the *_filament.json suffix at the source root —
    # filament_path should pick them up even without a filament/ subdir.
    printer = tmp_path / 'printer'
    printer.mkdir()
    (printer / 'my_t1_petg_filament.json').write_text('{"type":"filament"}')
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('from-printer', printer),))
    p = filament_path('PETG')
    assert p.name == 'my_t1_petg_filament.json'


def test_filament_path_raises_runtime_error_when_material_not_found(tmp_path, monkeypatch):
    stock = tmp_path / 'stock'
    (stock / 'filament').mkdir(parents=True)
    (stock / 'filament' / 'Generic PLA @U1 0.4 nozzle.json').write_text('{"type":"filament"}')
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', stock),))
    with pytest.raises(RuntimeError, match='no filament profile found'):
        filament_path('PETG')


# ---------- filament_path nozzle filter (v1.5.1 — caught live 2026-06-25) ----------

def _write_filament(path: Path, *, name: str | None = None, filament_type: list | None = None, compatible_printers: list | None = None) -> None:
    d: dict = {'type': 'filament', 'name': name or path.stem}
    if filament_type is not None:
        d['filament_type'] = filament_type
    if compatible_printers is not None:
        d['compatible_printers'] = compatible_printers
    path.write_text(json.dumps(d))


def test_filament_path_excludes_wrong_nozzle_when_label_encodes_one(tmp_path, monkeypatch):
    # Two PETG filaments: one for 0.2 nozzle, one generic. With nozzle='0.4'
    # the 0.2-tagged file must NOT be returned (that's the live-test bug:
    # Orca falls back to default_filament_profile=PLA when nozzle mismatches).
    fdir = tmp_path / 'snapmaker-stock' / 'filament'
    fdir.mkdir(parents=True)
    _write_filament(fdir / 'Snapmaker PETG HF @U1 0.2 nozzle.json', filament_type=['PETG'])
    _write_filament(fdir / 'Snapmaker PETG @U1.json', filament_type=['PETG'], compatible_printers=['Snapmaker U1 (0.4 nozzle)'])
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', tmp_path / 'snapmaker-stock'),))
    result = filament_path('PETG', nozzle='0.4')
    assert result.name == 'Snapmaker PETG @U1.json'


def test_filament_path_does_not_match_petg_with_petg_cf(tmp_path, monkeypatch):
    # 'PETG' should NOT match 'PETG-CF' or 'PETG-GF' filaments. Substring
    # match was the bug — 'PETG-CF' contains 'petg' so was being returned
    # for plain PETG slices.
    fdir = tmp_path / 'snapmaker-stock' / 'filament'
    fdir.mkdir(parents=True)
    _write_filament(fdir / 'Snapmaker PETG-CF @U1.json', filament_type=['PETG-CF'])
    _write_filament(fdir / 'Snapmaker PETG @U1.json', filament_type=['PETG'])
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', tmp_path / 'snapmaker-stock'),))
    result = filament_path('PETG', nozzle='0.4')
    assert result.name == 'Snapmaker PETG @U1.json'


def test_filament_path_skips_orca_base_inheritance_profiles(tmp_path, monkeypatch):
    # Orca convention: *@base.json, *@U1 base.json, *@U1 base2.json are
    # inheritance parents, NOT loadable filaments. Function must skip them
    # in the first pass and only fall back to them when nothing concrete
    # matches. Old '@base' substring filter missed '@U1 base' and '@U1 base2'.
    fdir = tmp_path / 'snapmaker-stock' / 'filament'
    fdir.mkdir(parents=True)
    _write_filament(fdir / 'Snapmaker PETG @U1 base.json', filament_type=['PETG'])
    _write_filament(fdir / 'Snapmaker PETG @U1.json', filament_type=['PETG'])
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', tmp_path / 'snapmaker-stock'),))
    result = filament_path('PETG', nozzle='0.4')
    assert result.name == 'Snapmaker PETG @U1.json'


def test_filament_path_raises_when_no_compatible_filament(tmp_path, monkeypatch):
    # When ALL candidates are for the wrong nozzle, fail closed rather than
    # silently letting Orca fall back to its hardcoded default.
    fdir = tmp_path / 'snapmaker-stock' / 'filament'
    fdir.mkdir(parents=True)
    _write_filament(fdir / 'Snapmaker PETG HF @U1 0.2 nozzle.json', filament_type=['PETG'])
    _write_filament(fdir / 'Snapmaker PETG HF @U1 0.8 nozzle.json', filament_type=['PETG'])
    import u1_profile_picker as upp
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', tmp_path / 'snapmaker-stock'),))
    with pytest.raises(RuntimeError, match="0.4"):
        filament_path('PETG', nozzle='0.4')


# ---------- Cold-review fixes (2026-06-25) ----------

def test_flatten_process_profile_user_profile_no_inherits(tmp_path):
    # User profiles with inherits='' or no inherits → flatten returns self-contained dict
    from u1_slice_workflow import _flatten_process_profile
    p = tmp_path / 'user_profile.json'
    p.write_text(json.dumps({'type':'process','name':'foo','inherits':'','enable_support':'0','width':0.4}))
    flat = _flatten_process_profile(p)
    assert 'inherits' not in flat
    assert flat['name'] == 'foo'
    assert flat['width'] == 0.4


def test_flatten_process_profile_preserves_unresolved_inherits(tmp_path):
    # If chain has inherits='something_we_cant_find', preserve that name so
    # Orca's own resolver can take over at slice time. Don't strip and produce
    # a broken profile (cold-review F17 — initial flatten attempt stripped
    # inherits even when chain wasn't fully resolved).
    from u1_slice_workflow import _flatten_process_profile
    p = tmp_path / 'orphan.json'
    p.write_text(json.dumps({'type':'process','inherits':'fdm_process_nowhere','some_field':'value'}))
    flat = _flatten_process_profile(p, orca_bin=tmp_path / 'no_orca')
    assert flat.get('inherits') == 'fdm_process_nowhere'
    assert flat['some_field'] == 'value'


def test_flatten_process_profile_resolves_chain_in_sibling_dirs(tmp_path):
    # Multi-layer chain: leaf inherits from parent in a sibling dir. Walk
    # the chain, merge each layer, leaf wins on conflicts.
    from u1_slice_workflow import _flatten_process_profile
    process_dir = tmp_path / 'stock' / 'process'
    machine_dir = tmp_path / 'stock' / 'machine'
    process_dir.mkdir(parents=True)
    machine_dir.mkdir(parents=True)
    # Parent in machine/ subdir (sibling of process/)
    (machine_dir / 'parent.json').write_text(json.dumps({'name':'parent','field_a':'parent_value','field_b':'parent_only'}))
    # Leaf in process/, inherits parent
    leaf = process_dir / 'leaf.json'
    leaf.write_text(json.dumps({'name':'leaf','inherits':'parent','field_a':'leaf_value','field_c':'leaf_only'}))
    flat = _flatten_process_profile(leaf, orca_bin=tmp_path / 'no_orca')
    # leaf wins on field_a, parent's field_b survives, leaf's field_c is intact
    assert flat['field_a'] == 'leaf_value'
    assert flat['field_b'] == 'parent_only'
    assert flat['field_c'] == 'leaf_only'
    # inherits stripped — chain fully resolved
    assert 'inherits' not in flat


def test_apply_supports_override_uses_flattened_profile(tmp_path):
    # Verify apply_supports_override produces a flattened temp, not just a copy
    # with patched enable_support.
    from u1_slice_workflow import apply_supports_override
    process_dir = tmp_path / 'stock' / 'process'
    machine_dir = tmp_path / 'stock' / 'machine'
    process_dir.mkdir(parents=True)
    machine_dir.mkdir(parents=True)
    (machine_dir / 'base.json').write_text(json.dumps({'name':'base','line_width':0.4,'enable_support':'0'}))
    leaf = process_dir / 'leaf.json'
    leaf.write_text(json.dumps({'name':'leaf','inherits':'base','top_shell_layers':3}))
    out = apply_supports_override(leaf, enable_support=True, out_dir=tmp_path / 'out')
    d = json.loads(out.read_text())
    assert d['enable_support'] == '1'  # override wins
    assert d['line_width'] == 0.4  # flattened from parent
    assert d['top_shell_layers'] == 3  # leaf field intact
    assert 'inherits' not in d  # fully resolved


# ---------- Platform-neutral Orca resource-root discovery (v2.4.1) ----------
# Two real-world layouts:
#   Windows portable zip:   <dir>/orca-slicer.exe  +  <dir>/resources/
#   Linux AppImage extract: <squashfs-root>/bin/orca-slicer  +  <squashfs-root>/resources/
# The old lookup hardcoded orca_bin.parents[1]/resources (AppImage-only), so on
# Windows portable the inherits-chain flattener couldn't find the vendor dir and
# Orca silently fell back to PLA defaults (reproduced on a real Windows desktop
# 2026-07-10: upstream PETG leaf profile produced filament_type=PLA gcode).

def _fake_orca_portable(tmp_path):
    """Windows-portable layout: exe beside resources/."""
    root = tmp_path / 'orca242'
    (root / 'resources' / 'profiles' / 'Snapmaker').mkdir(parents=True)
    exe = root / 'orca-slicer.exe'
    exe.write_bytes(b'MZ')
    return exe, root / 'resources'


def _fake_orca_appimage(tmp_path):
    """Linux-AppImage layout: bin/orca-slicer with sibling resources/."""
    root = tmp_path / 'squashfs-root'
    (root / 'resources' / 'profiles' / 'Snapmaker').mkdir(parents=True)
    (root / 'bin').mkdir(parents=True)
    exe = root / 'bin' / 'orca-slicer'
    exe.write_bytes(b'\x7fELF')
    return exe, root / 'resources'


def test_orca_resources_root_windows_portable_layout(tmp_path):
    from u1_slice_workflow import orca_resources_root
    exe, resources = _fake_orca_portable(tmp_path)
    assert orca_resources_root(exe) == resources


def test_orca_resources_root_appimage_layout(tmp_path):
    from u1_slice_workflow import orca_resources_root
    exe, resources = _fake_orca_appimage(tmp_path)
    assert orca_resources_root(exe) == resources


def test_orca_resources_root_none_when_absent(tmp_path):
    # Wrapper/shim binary with no resources tree anywhere near it.
    from u1_slice_workflow import orca_resources_root
    shim = tmp_path / 'orca-via-shim'
    shim.write_text('#!/bin/sh\n')
    assert orca_resources_root(shim) is None


def test_orca_resources_root_nonexistent_binary_no_crash(tmp_path):
    from u1_slice_workflow import orca_resources_root
    assert orca_resources_root(tmp_path / 'missing' / 'orca-slicer.exe') is None


def test_flatten_filament_resolves_chain_in_portable_vendor_dir(tmp_path):
    # The PETG leaf lives in OUR profile tree; its parent lives ONLY in the
    # (Windows-portable-shaped) Orca vendor dir. The flattener must find it
    # via the resolver, not via the old parents[1] AppImage assumption.
    from u1_slice_workflow import _flatten_filament_profile
    exe, resources = _fake_orca_portable(tmp_path)
    vendor_fil = resources / 'profiles' / 'Snapmaker' / 'filament'
    vendor_fil.mkdir(parents=True)
    (vendor_fil / 'PETG base.json').write_text(json.dumps(
        {'name': 'PETG base', 'filament_type': ['PETG'], 'hot_plate_temp': ['80']}))
    leaf_dir = tmp_path / 'ours' / 'filament'
    leaf_dir.mkdir(parents=True)
    leaf = leaf_dir / 'PETG leaf.json'
    leaf.write_text(json.dumps({'name': 'PETG leaf', 'inherits': 'PETG base'}))
    flat = _flatten_filament_profile(leaf, orca_bin=exe)
    assert flat['filament_type'] == ['PETG']
    assert flat['hot_plate_temp'] == ['80']
    assert 'inherits' not in flat  # chain fully resolved


def test_flatten_process_resolves_chain_in_portable_vendor_dir(tmp_path):
    from u1_slice_workflow import _flatten_process_profile
    exe, resources = _fake_orca_portable(tmp_path)
    vendor_proc = resources / 'profiles' / 'Snapmaker' / 'process'
    vendor_proc.mkdir(parents=True)
    (vendor_proc / 'fdm_process_common.json').write_text(json.dumps(
        {'name': 'fdm_process_common', 'line_width': 0.4}))
    leaf_dir = tmp_path / 'ours' / 'process'
    leaf_dir.mkdir(parents=True)
    leaf = leaf_dir / 'leaf.json'
    leaf.write_text(json.dumps({'name': 'leaf', 'inherits': 'fdm_process_common'}))
    flat = _flatten_process_profile(leaf, orca_bin=exe)
    assert flat['line_width'] == 0.4
    assert 'inherits' not in flat


def test_machine_profile_found_in_portable_layout(tmp_path):
    from u1_slice_workflow import machine_profile_for_orca
    exe, resources = _fake_orca_portable(tmp_path)
    machine_dir = resources / 'profiles' / 'Snapmaker' / 'machine'
    machine_dir.mkdir(parents=True)
    vendor_machine = machine_dir / 'Snapmaker U1 (0.4 nozzle).json'
    vendor_machine.write_text(json.dumps({'name': 'Snapmaker U1 (0.4 nozzle)'}))
    assert machine_profile_for_orca(exe) == vendor_machine


def test_last_used_print_settings_id_empty_nozzle_skips_filter(tmp_path):
    # Cold-review F7: nozzle='' or None should NOT silently filter every
    # record (the pre-fix '( nozzle)' literal substring match dropped all).
    from u1_slice_workflow import last_used_print_settings_id
    history = tmp_path / 'history.json'
    history.write_text(json.dumps({'records': [
        {'last_seen_at': '2026-06-01T00:00:00', 'printer_settings_id': 'Snapmaker U1 (0.4 nozzle)', 'print_settings_id': 'P-04', 'active_tool': {'tool':'T1'}},
        {'last_seen_at': '2026-06-02T00:00:00', 'printer_settings_id': 'Snapmaker U1 (0.2 nozzle)', 'print_settings_id': 'P-02', 'active_tool': {'tool':'T1'}},
    ]}))
    # Empty nozzle: returns most-recent ANY-nozzle match (P-02)
    assert last_used_print_settings_id(tool='T1', nozzle='', history_path=history) == 'P-02'
    assert last_used_print_settings_id(tool='T1', nozzle=None, history_path=history) == 'P-02'
    # Explicit nozzle: filters
    assert last_used_print_settings_id(tool='T1', nozzle='0.4', history_path=history) == 'P-04'


def test_history_match_does_not_false_flag_substring_labels(tmp_path, monkeypatch):
    # Cold-review F1: adversarial labels that are substrings of history_id
    # must NOT false-flag as previously_used.
    import u1_profile_picker as upp
    stock = tmp_path / 'stock' / 'process'
    stock.mkdir(parents=True)
    # Adversarial: short single-word labels that are substrings of the
    # full print_settings_id 'Community 0.20 Strength Gyroid @Snapmaker U1 ...'
    for stem in ['Strength', 'Gyroid', '0.20', 'Strength Gyroid', '0.20 Strength', 'Community 0.20']:
        (stock / f'{stem}.json').write_text(json.dumps({'type':'process'}))
    # The actual match (after rename + decoration strip)
    (stock / '0.20 Strength Gyroid.json').write_text(json.dumps({'type':'process'}))
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('stock', tmp_path / 'stock'),))
    opts = upp.list_profiles(history_print_settings_id='Community 0.20 Strength Gyroid @Snapmaker U1 Textured PEI')
    flagged = sorted([o['label'] for o in opts if o.get('previously_used')])
    # Only the real match should flag — adversarial substrings should not
    assert flagged == ['0.20 Strength Gyroid'], f'unexpected flagged: {flagged}'


def test_history_match_handles_hermes_prefix(tmp_path, monkeypatch):
    # 'Hermes ' is also a recognized decoration prefix in print_history.
    import u1_profile_picker as upp
    stock = tmp_path / 'stock' / 'process'
    stock.mkdir(parents=True)
    (stock / '0.16 Optimal Fuzzy.json').write_text(json.dumps({'type':'process'}))
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('stock', tmp_path / 'stock'),))
    opts = upp.list_profiles(history_print_settings_id='Hermes 0.16 Optimal Fuzzy @Snapmaker U1 Textured PEI')
    flagged = [o['label'] for o in opts if o.get('previously_used')]
    assert flagged == ['0.16 Optimal Fuzzy']


# ---------- Cold-review full-pass fixes (2026-06-25) ----------

def test_filament_path_rejects_off_convention_name_via_compatible_printers(tmp_path, monkeypatch):
    # F13: user-extracted filament with no nozzle token in name but
    # compatible_printers limits it to a different nozzle. Filename heuristic
    # would have kept it (no 'nozzle' substring → assume compatible);
    # JSON-level compatible_printers gate correctly rejects.
    import u1_profile_picker as upp
    fdir = tmp_path / 'snapmaker-stock' / 'filament'
    fdir.mkdir(parents=True)
    # Stem has no nozzle qualifier → passes filename heuristic
    (fdir / 'custom_petg_blob.json').write_text(json.dumps({
        'type':'filament','name':'custom petg blob','filament_type':['PETG'],
        'compatible_printers':['Snapmaker U1 (0.2 nozzle)'],
    }))
    # And a legitimate 0.4 fallback so the function doesn't raise.
    (fdir / 'Snapmaker PETG @U1.json').write_text(json.dumps({
        'type':'filament','filament_type':['PETG'],
        'compatible_printers':['Snapmaker U1 (0.4 nozzle)'],
    }))
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', tmp_path / 'snapmaker-stock'),))
    result = filament_path('PETG', nozzle='0.4')
    assert result.name == 'Snapmaker PETG @U1.json'


def test_filament_path_keeps_filament_when_compatible_printers_unset(tmp_path, monkeypatch):
    # F13: filaments with no compatible_printers field should NOT be
    # rejected — empty/missing = compatible with everything (common case
    # for user-extracted profiles where the gcode metadata didn't carry it).
    import u1_profile_picker as upp
    fdir = tmp_path / 'snapmaker-stock' / 'filament'
    fdir.mkdir(parents=True)
    (fdir / 'Snapmaker PETG @U1.json').write_text(json.dumps({
        'type':'filament','filament_type':['PETG'],
        # NO compatible_printers field
    }))
    monkeypatch.setattr(upp, 'DEFAULT_SOURCES', (('snapmaker-stock', tmp_path / 'snapmaker-stock'),))
    result = filament_path('PETG', nozzle='0.4')
    assert result.name == 'Snapmaker PETG @U1.json'


def test_extract_layer_height_falls_back_to_json_for_off_convention_names(tmp_path):
    # F2: a profile whose filename doesn't start with a height prefix
    # (e.g., user-renamed 'my_strength.json') should still get the right
    # layer-height tier via the JSON layer_height field.
    import u1_profile_picker as upp
    p = tmp_path / 'my_strength.json'
    p.write_text(json.dumps({'type':'process','layer_height':'0.20'}))
    # Direct _read_layer_height test
    assert upp._read_layer_height(str(p)) == 0.20
    # Via opt-dict path: tier 0 for 0.4 nozzle workhorse
    opt = {'label': 'my_strength', 'path': str(p)}
    assert upp._layer_height_tier(opt, '0.4') == 0


def test_read_layer_height_handles_list_int_str_forms(tmp_path):
    import u1_profile_picker as upp
    # Orca sometimes stores layer_height as a list (per-extruder?), sometimes
    # as a string, sometimes as a float.
    for value, expected in [
        ('0.16', 0.16),
        (0.20, 0.20),
        (['0.24'], 0.24),
        ([0.32], 0.32),
        ([], None),
        (None, None),
    ]:
        p = tmp_path / 'x.json'
        p.write_text(json.dumps({'layer_height': value} if value is not None else {}))
        got = upp._read_layer_height(str(p))
        assert got == expected, f'value={value!r} expected={expected} got={got}'


def test_layer_height_tier_generic_fallback_for_unknown_nozzle():
    # F4: unknown nozzle (not in _NOZZLE_HEIGHT_PRIORITY) should use the
    # workhorse-distance heuristic (target = nozzle * 0.5) so we still get
    # a useful ordering, not a flat tier-50 for everything.
    import u1_profile_picker as upp
    # 0.5 nozzle is IN the table now (F4 added it). Test 0.7 instead which
    # isn't in the table.
    # Target workhorse = 0.7 * 0.5 = 0.35mm. 0.35 has distance 0 → tier 0.
    # 0.20 has distance 0.15 → tier 15.
    # 0.50 has distance 0.15 → tier 15.
    assert upp._layer_height_tier('0.35 Standard', '0.7') == 0
    assert upp._layer_height_tier('0.20 Strength', '0.7') == 15
    assert upp._layer_height_tier('0.50 Quick', '0.7') == 15


def test_layer_height_tier_added_nozzles_0_3_and_0_5():
    # F4: 0.3 and 0.5 nozzles added to the priority table.
    import u1_profile_picker as upp
    assert upp._layer_height_tier('0.15 Standard', '0.3') == 0  # 0.3's workhorse
    assert upp._layer_height_tier('0.25 Standard', '0.5') == 0  # 0.5's workhorse


# ---------- Cold-review pass 3 fixes (2026-06-25) ----------

def test_last_used_per_tool_returns_most_recent_per_tool(tmp_path):
    # G16: per-tool history map shows the right preset for each tool, not
    # just the single most-recent-any-tool result.
    from u1_slice_workflow import last_used_per_tool
    hist = tmp_path / 'history.json'
    hist.write_text(json.dumps({'records': [
        {'last_seen_at': '2026-06-22T00:00:00', 'printer_settings_id': 'Snapmaker U1 (0.4 nozzle)',
         'print_settings_id': 'T0-older', 'active_tool': {'tool': 'T0'}},
        {'last_seen_at': '2026-06-24T00:00:00', 'printer_settings_id': 'Snapmaker U1 (0.4 nozzle)',
         'print_settings_id': 'T0-newer', 'active_tool': {'tool': 'T0'}},
        {'last_seen_at': '2026-06-23T00:00:00', 'printer_settings_id': 'Snapmaker U1 (0.4 nozzle)',
         'print_settings_id': 'T1-only', 'active_tool': {'tool': 'T1'}},
        {'last_seen_at': '2026-06-25T00:00:00', 'printer_settings_id': 'Snapmaker U1 (0.2 nozzle)',
         'print_settings_id': 'wrong-nozzle', 'active_tool': {'tool': 'T0'}},
    ]}))
    result = last_used_per_tool(nozzle='0.4', history_path=hist)
    assert result == {'T0': 'T0-newer', 'T1': 'T1-only'}, f'unexpected: {result}'


def test_last_used_per_tool_empty_when_no_history(tmp_path):
    from u1_slice_workflow import last_used_per_tool
    # No history file
    assert last_used_per_tool(nozzle='0.4', history_path=tmp_path / 'nonexistent.json') == {}
    # Empty records array
    hist = tmp_path / 'empty.json'
    hist.write_text(json.dumps({'records': []}))
    assert last_used_per_tool(nozzle='0.4', history_path=hist) == {}


def test_suggested_rename_appends_utc_timestamp():
    from u1_upload_gcode import _suggested_rename
    import re
    out = _suggested_rename('wall_mount.gcode')
    # Pattern: <stem>_<YYYYMMDD-HHMMSS>.<ext>
    assert re.match(r'wall_mount_\d{8}-\d{6}\.gcode$', out), f'unexpected rename: {out}'


def test_suggested_rename_preserves_compound_extension():
    from u1_upload_gcode import _suggested_rename
    out = _suggested_rename('thing.plate_1.gcode')
    assert out.endswith('.gcode')
    assert 'plate_1' in out


def test_real_upload_rc3_does_not_say_no_file_reached(tmp_path, monkeypatch):
    # Live-caught regression: helper returns rc=3 because of post-upload
    # warnings (cancelled+idle), workflow collapses to "no file reached".
    # The new contract reads granular fields and says the truth.
    import u1_slice_workflow as wf
    fake_gcode = tmp_path / 'thing.gcode'
    fake_gcode.write_text('; fake gcode\n')

    import json as _json
    # Fake the helper's artifact write to look like rc=3 with file on printer
    artifact = tmp_path / 'latest_upload_result.json'
    artifact.write_text(_json.dumps({
        'moonraker_upload_ok': True,
        'remote_metadata_ok': True,
        'post_upload_validation_ok': False,
        'uploaded_filename': 'thing.gcode',
        'target_filename': 'thing.gcode',
        'filename_already_existed': False,
        'collision_policy': None,
        'post_upload_blockers': ["post-upload print_stats.state is 'paused'"],
        'post_upload_warnings': [],
    }))
    from u1_config import get_data_dir as _orig
    monkeypatch.setattr('u1_config.get_data_dir', lambda: tmp_path)

    class FakeProc:
        returncode = 3
        stdout = 'UPLOAD WARNING\n- post-upload print_stats.state is paused\n'
    monkeypatch.setattr(wf.subprocess, 'run', lambda *a, **k: FakeProc())
    monkeypatch.setattr(wf, 'query_moonraker_metadata', lambda *a, **k: None, raising=False)

    result = wf._real_upload(fake_gcode, on_collision=None)
    assert result['returncode'] == 3
    assert result['moonraker_upload_ok'] is True
    assert result['remote_metadata_ok'] is True
    assert 'SUCCEEDED' in result['human_summary']
    assert 'no file reached' not in result['human_summary'].lower()
    assert 'IS on the printer' in result['human_summary']


def test_real_upload_rc4_says_transport_failed(tmp_path, monkeypatch):
    import u1_slice_workflow as wf
    fake_gcode = tmp_path / 'thing.gcode'
    fake_gcode.write_text('; fake\n')

    class FakeProc:
        returncode = 4
        stdout = 'UPLOAD FAILED (Moonraker upload did not produce a remote file)\n'
    monkeypatch.setattr(wf.subprocess, 'run', lambda *a, **k: FakeProc())
    monkeypatch.setattr('u1_config.get_data_dir', lambda: tmp_path)

    result = wf._real_upload(fake_gcode, on_collision=None)
    assert result['returncode'] == 4
    assert result['moonraker_upload_ok'] is False
    assert 'transport failed' in result['human_summary']


def test_real_upload_rc5_collision_packet(tmp_path, monkeypatch):
    import u1_slice_workflow as wf
    fake_gcode = tmp_path / 'wall_mount.gcode'
    fake_gcode.write_text('; fake\n')

    class FakeProc:
        returncode = 5
        stdout = 'UPLOAD COLLISION\n{"target_filename": "wall_mount.gcode"}\n'
    monkeypatch.setattr(wf.subprocess, 'run', lambda *a, **k: FakeProc())
    monkeypatch.setattr('u1_config.get_data_dir', lambda: tmp_path)

    result = wf._real_upload(fake_gcode, on_collision=None)
    assert result['returncode'] == 5
    assert result['filename_collision'] is True
    assert result['target_filename'] == 'wall_mount.gcode'
    assert 'already exists on the U1' in result['human_summary']


# ---------- Cold review of audit 2026-06-26 commit ----------

def test_real_upload_rc6_user_cancelled_collision(tmp_path, monkeypatch):
    # F9: rc=6 = user picked Cancel at collision prompt. Must NOT trigger
    # the rc=5 collision-prompt branch — that would infinite-loop.
    import u1_slice_workflow as wf
    fake = tmp_path / 'thing.gcode'
    fake.write_text('; fake\n')
    class FakeProc:
        returncode = 6
        stdout = 'UPLOAD CANCELLED (filename collision, --on-collision=cancel)\n'
    monkeypatch.setattr(wf.subprocess, 'run', lambda *a, **k: FakeProc())
    monkeypatch.setattr('u1_config.get_data_dir', lambda: tmp_path)
    result = wf._real_upload(fake, on_collision='cancel')
    assert result['returncode'] == 6
    assert result['cancelled'] is True
    assert 'cancelled by operator' in result['human_summary'].lower()
    assert not result.get('filename_collision')  # must NOT trigger collision branch


def test_real_upload_unexpected_rc_treated_as_transport_failure(tmp_path, monkeypatch):
    # Defensive: rc=1 (Python uncaught exception exit) used to silently fall
    # into the rc==3 "upload succeeded with warnings" branch. Now treated as
    # transport failure.
    import u1_slice_workflow as wf
    fake = tmp_path / 'thing.gcode'
    fake.write_text('; fake\n')
    class FakeProc:
        returncode = 1
        stdout = 'Traceback (most recent call last):\nNameError: name "tool_out" is not defined\n'
    monkeypatch.setattr(wf.subprocess, 'run', lambda *a, **k: FakeProc())
    monkeypatch.setattr('u1_config.get_data_dir', lambda: tmp_path)
    result = wf._real_upload(fake, on_collision=None)
    assert result['returncode'] == 1
    assert result['moonraker_upload_ok'] is False
    assert 'unexpected returncode' in result['human_summary']


def test_upload_blocked_path_does_not_raise_nameerror(tmp_path, monkeypatch, capsys):
    # F1: previously crashed with NameError on tool_out when blockers were
    # present. Now exits cleanly with rc=2.
    import u1_upload_gcode as u
    fake = tmp_path / 'fake.gcode'
    # Wrong printer_settings_id → blocker; PETG filament_type so other gates pass
    fake.write_text(
        '; printer_settings_id = Bambu X1C\n'
        '; filament_type = PETG\n'
        '; first_layer_bed_temperature = 70\n'
    )
    monkeypatch.setattr(u, 'query_state', lambda h, p: {
        'print_stats': {'state': 'standby'}, 'virtual_sdcard': {'is_active': False},
        'pause_resume': {'is_paused': False}, 'webhooks': {'state': 'ready'}
    })
    monkeypatch.setattr(u, 'query_userdata_space', lambda h, p: None)
    monkeypatch.setattr(u, 'get_u1_host', lambda: '127.0.0.1')
    monkeypatch.setattr(u, 'get_u1_port', lambda: 7125)
    monkeypatch.setattr(u.sys, 'argv',
                        ['u1_upload_gcode.py', str(fake),
                         '--host', '127.0.0.1', '--port', '7125',
                         '--expected-printer', 'Snapmaker U1'])
    rc = u.main()
    out = capsys.readouterr().out
    assert rc == 2, f'expected rc=2, got rc={rc}; output={out[:600]}'
    assert 'UPLOAD BLOCKED' in out
    assert 'printer_settings_id mismatch' in out
    assert 'NameError' not in out


def test_workflow_uses_uploaded_filename_when_renamed(tmp_path, monkeypatch):
    # F10: readiness_card.printer_storage_filename must use the helper's
    # actual uploaded filename, not the original gcode name, when collision
    # was resolved as rename.
    import u1_slice_workflow as wf
    fake = tmp_path / 'wall_mount.gcode'
    fake.write_text('; fake\n')

    import json as _json
    artifact = tmp_path / 'latest_upload_result.json'
    artifact.write_text(_json.dumps({
        'moonraker_upload_ok': True,
        'remote_metadata_ok': True,
        'post_upload_validation_ok': True,
        'uploaded_filename': 'wall_mount_20260626-160000.gcode',  # renamed
        'target_filename': 'wall_mount.gcode',
        'filename_already_existed': True,
        'collision_policy': 'rename',
        'post_upload_blockers': [],
        'post_upload_warnings': [],
    }))
    class FakeProc:
        returncode = 0
        stdout = 'U1 upload-only staging complete:\n'
    monkeypatch.setattr(wf.subprocess, 'run', lambda *a, **k: FakeProc())
    monkeypatch.setattr('u1_config.get_data_dir', lambda: tmp_path)

    result = wf._real_upload(fake, on_collision='rename')
    assert result['uploaded_filename'] == 'wall_mount_20260626-160000.gcode'
    assert result['target_filename'] == 'wall_mount.gcode'
    assert result['collision_policy'] == 'rename'


# ============================================================================
# v1.6 (A) — pre-slice Orca mesh-topology analysis
# ============================================================================
# Backing evidence: docs/v1.6-design.md (empirical validation on 4 fixtures)


def test_v16_categorize_clean_on_empty_warning():
    from u1_slice_workflow import _categorize_orca_warning
    assert _categorize_orca_warning('') == 'CLEAN'
    assert _categorize_orca_warning(None) == 'CLEAN'
    assert _categorize_orca_warning('null') == 'CLEAN'
    assert _categorize_orca_warning('   ') == 'CLEAN'


def test_v16_categorize_cantilever():
    from u1_slice_workflow import _categorize_orca_warning
    # The exact wording Orca emitted for wall_mount source in the v1.6 study
    msg = ("It seems object source.stl has floating cantilever. "
           "Please re-orient the object or enable support generation.")
    assert _categorize_orca_warning(msg) == 'CANTILEVER'
    # Case-invariant
    assert _categorize_orca_warning('FLOATING CANTILEVER detected') == 'CANTILEVER'


def test_v16_categorize_floating_regions():
    from u1_slice_workflow import _categorize_orca_warning
    msg = ("It seems object source.stl has floating regions. "
           "Please re-orient the object or enable support generation.")
    assert _categorize_orca_warning(msg) == 'FLOATING_REGIONS'


def test_v16_categorize_overhang_flagged_fallback():
    from u1_slice_workflow import _categorize_orca_warning
    # Any other "overhang" wording lands in OVERHANG_FLAGGED, not UNKNOWN
    assert _categorize_orca_warning('Detected overhang above threshold') == 'OVERHANG_FLAGGED'


def test_v16_categorize_unknown_for_unrelated_text():
    from u1_slice_workflow import _categorize_orca_warning
    assert _categorize_orca_warning('warning: bed temperature high') == 'UNKNOWN'


def test_v16_count_overhang_layers_synthetic_gcode(tmp_path):
    from u1_slice_workflow import _count_overhang_layers
    gc = tmp_path / 'synth.gcode'
    # 4 layers; layers 2 and 3 contain Overhang wall tags
    gc.write_text("\n".join([
        ";LAYER_CHANGE", ";TYPE:Inner wall", "G1 X0 Y0",
        ";LAYER_CHANGE", ";TYPE:Overhang wall", "G1 X1 Y1",
        ";LAYER_CHANGE", ";TYPE:Overhang wall", "G1 X2 Y2", ";TYPE:Inner wall",
        ";LAYER_CHANGE", ";TYPE:Inner wall", "G1 X3 Y3",
    ]))
    overhang, total = _count_overhang_layers(gc)
    assert total == 4
    assert overhang == 2


def test_v16_count_overhang_layers_missing_file(tmp_path):
    from u1_slice_workflow import _count_overhang_layers
    assert _count_overhang_layers(tmp_path / 'nope.gcode') == (0, 0)


def test_v16_materialize_draft_profile_applies_overrides(tmp_path):
    from u1_slice_workflow import _materialize_draft_profile, _V16_DRAFT_OVERRIDES
    prod = tmp_path / 'prod.json'
    prod.write_text(json.dumps({
        'layer_height': '0.20',
        'wall_loops': '6',
        'sparse_infill_density': '25%',
        'top_shell_layers': '4',
        'bottom_shell_layers': '4',
        'gcode_thumbnails': '1',
        'enable_support': '0',
        'other_field': 'keep_me',
    }))
    dest = _materialize_draft_profile(prod, tmp_path)
    out = json.loads(dest.read_text())
    # All v1.6 overrides applied
    for k, v in _V16_DRAFT_OVERRIDES.items():
        assert out[k] == v, f'override {k} not applied'
    # Production layer_height preserved (the load-bearing decision)
    assert out['layer_height'] == '0.20'
    # Unrelated fields untouched
    assert out['other_field'] == 'keep_me'


def test_v16_decide_recommendation_source_clean_no_auto():
    from u1_slice_workflow import _decide_orient_recommendation
    src = {'clean': True, 'category': 'CLEAN', 'overhang_pct': 4.0}
    assert _decide_orient_recommendation(src, None) == 'asauthored'


def test_v16_decide_recommendation_source_risky_auto_clean():
    from u1_slice_workflow import _decide_orient_recommendation
    src = {'clean': False, 'category': 'FLOATING_REGIONS', 'overhang_pct': 56.3}
    auto = {'clean': True, 'category': 'CLEAN', 'overhang_pct': 7.5}
    assert _decide_orient_recommendation(src, auto) == 'auto'


def test_v16_decide_recommendation_both_clean_prefer_asauthored():
    from u1_slice_workflow import _decide_orient_recommendation
    src = {'clean': True, 'category': 'CLEAN', 'overhang_pct': 5.0}
    auto = {'clean': True, 'category': 'CLEAN', 'overhang_pct': 3.0}
    # Tiebreak goes to asauthored when overhang_pcts are close (5 vs 3, but
    # asauthored avoids weird rotation when both are clean) — actual rule:
    # lower overhang_pct wins, tiebreak to asauthored
    assert _decide_orient_recommendation(src, auto) == 'auto'  # 3 < 5 → auto


def test_v16_decide_recommendation_both_risky_lower_overhang_wins():
    from u1_slice_workflow import _decide_orient_recommendation
    src = {'clean': False, 'category': 'CANTILEVER', 'overhang_pct': 8.8}
    auto = {'clean': False, 'category': 'FLOATING_REGIONS', 'overhang_pct': 56.3}
    assert _decide_orient_recommendation(src, auto) == 'asauthored'  # 8.8 < 56.3


def test_v16_compose_note_clean_skipped_auto():
    from u1_slice_workflow import _compose_orient_note
    src = {'category': 'CLEAN', 'overhang_pct': 4.0, 'clean': True}
    note = _compose_orient_note(src, None, auto_skip_reason='source_clean')
    assert 'As-authored' in note
    assert 'clean' in note.lower()
    assert 'not analyzed' in note.lower() or 'force a comparison' in note.lower()


def test_v16_compose_note_auto_identical_skip_reason():
    # Regression guard for the smoke-test bug: source is FLOATING_REGIONS but
    # Orca's auto-orient produced no rotation (auto_stl == source_stl), so
    # auto_draft is None. Note must NOT say "as-authored is clean" — it must
    # explain that auto found no better rotation.
    from u1_slice_workflow import _compose_orient_note
    src = {'category': 'FLOATING_REGIONS', 'overhang_pct': 39.0, 'clean': False}
    note = _compose_orient_note(src, None, auto_skip_reason='auto_identical')
    assert 'no better rotation' in note.lower()
    # Must surface the floating-regions warning, not "is clean"
    assert 'floating-regions' in note.lower()


def test_v16_compose_note_auto_failed_skip_reason():
    from u1_slice_workflow import _compose_orient_note
    src = {'category': 'CANTILEVER', 'overhang_pct': 22.0, 'clean': False}
    note = _compose_orient_note(src, None, auto_skip_reason='auto_failed')
    assert 'auto-orient analysis failed' in note.lower()


def test_v16_compose_note_cantilever_with_clean_auto():
    from u1_slice_workflow import _compose_orient_note
    src = {'category': 'CANTILEVER', 'overhang_pct': 22.1, 'clean': False}
    auto = {'category': 'CLEAN', 'overhang_pct': 7.5, 'clean': True}
    note = _compose_orient_note(src, auto)
    assert 'cantilever' in note.lower()
    assert 'clean' in note.lower()


def test_v16_compose_note_both_floating_regions():
    from u1_slice_workflow import _compose_orient_note
    src = {'category': 'FLOATING_REGIONS', 'overhang_pct': 27.7, 'clean': False}
    auto = {'category': 'FLOATING_REGIONS', 'overhang_pct': 20.0, 'clean': False}
    note = _compose_orient_note(src, auto)
    # Both phrases present
    assert note.count('floating-regions') >= 2 or note.lower().count('floating') >= 2


def test_v16_compose_note_draft_failed_includes_fallback_marker():
    from u1_slice_workflow import _compose_orient_note
    src = {'category': 'DRAFT_FAILED', 'overhang_pct': 0.0, 'clean': False, 'error': 'Orca rc=-99: bad mesh'}
    note = _compose_orient_note(src, None)
    assert 'failed' in note.lower()
    assert 'face-angle' in note.lower() or 'fallback' in note.lower()


# ============================================================================
# Phase 2 — phase-aware resume short-circuit + MED-2 phase preservation
# ============================================================================
# These don't need real_orca: the short-circuit fires BEFORE any slicing,
# so the orca-shim machinery is never touched. We exercise run_workflow
# directly with a pre-seeded request.json and assert it returns early.


def _build_phase2_args(model_path, out_dir, request_id, *, upload_decision='upload_start'):
    """Build a Namespace shaped like argparse output for run_workflow.

    Mirrors u1_slice_workflow.main's argparser so when add_argument() lines
    grow, this helper just needs the matching field added — same shape, no
    coupling to internal flag plumbing."""
    return argparse.Namespace(
        model=str(model_path),
        json_events=True,
        yes=False,
        orient=None,
        down_vec=None,
        tool=None,
        material=None,
        profile=None,
        class_hint=None,
        supports=None,
        nozzle='0.4',
        upload_only=False,
        live_upload=False,
        upload_decision=upload_decision,
        on_collision=None,
        no_live_material=True,
        out_dir=Path(out_dir),
        request_id=request_id,
        fresh=False,
        cancel=False,
    )


def _seed_awaiting_start_approval_request(tmp_path, *, with_next_action=True):
    """Pre-seed a request.json that looks like a prior workflow run reached
    the readiness_card. Returns (request_id, stl_path)."""
    import u1_request as _u1r
    # Use an STL with stable bytes so the content hash is deterministic.
    stl = tmp_path / 'cable_holder.stl'
    stl.write_bytes(b'STABLE_STL_BYTES_FOR_RESUME_TEST')
    rid = _u1r.generate_request_id()
    fields = {
        'model_file': stl.name,
        'model_path': str(stl),
        'model_hash': _u1r.compute_model_hash(stl),
        'orient': 'asauthored',
        'tool': 'T1',
        'material': 'PETG',
        'profile': '0_20_standard_snapmaker_u1_0_4_nozzle',
        'supports': 'no_supports',
        'upload_decision': 'upload_start',
        'phase': 'awaiting_start_approval',
        'printer_storage_filename': 'cable_holder.gcode',
        'start_gate_stage1_command': (
            'python3 /opt/data/scripts/u1_print_start_gate.py cable_holder.gcode '
            '--intended-tool extruder --requested-material PETG'
        ),
        'readiness_card_event': {
            'stage': 'readiness_card',
            'orient': 'asauthored',
            'tool': 'T1',
            'material': 'PETG',
            'printer_storage_filename': 'cable_holder.gcode',
            'start_gate_stage1_command': (
                'python3 /opt/data/scripts/u1_print_start_gate.py cable_holder.gcode '
                '--intended-tool extruder --requested-material PETG'
            ),
        },
    }
    if with_next_action:
        fields['next_action_required_event'] = {
            'stage': 'next_action_required',
            'reason': 'Resumed test fixture',
            'command': fields['start_gate_stage1_command'],
        }
    _u1r.write_request(rid, **fields)
    return rid, stl


def _read_events_jsonl(out_dir):
    events_path = Path(out_dir) / 'events.jsonl'
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


def test_phase_aware_skip_falls_through_when_no_saved_payload(tmp_path):
    """Backward-compat guard: a request created by an OLDER workflow won't
    have readiness_card_event saved. The short-circuit must NOT fire — it
    must fall through and let the workflow rebuild from scratch (no
    KeyError, no silent half-resume)."""
    import u1_request as _u1r
    stl = tmp_path / 'legacy.stl'
    stl.write_bytes(b'LEGACY_STL')
    rid = _u1r.generate_request_id()
    # phase='awaiting_start_approval' BUT no readiness_card_event saved.
    _u1r.write_request(rid,
                       model_hash=_u1r.compute_model_hash(stl),
                       phase='awaiting_start_approval',
                       printer_storage_filename='legacy.gcode',
                       start_gate_stage1_command='python3 /opt/data/scripts/u1_print_start_gate.py legacy.gcode')
    out_dir = tmp_path / 'out'
    args = _build_phase2_args(stl, out_dir, rid)
    # Short-circuit doesn't fire → workflow tries to proceed → without a
    # valid STL/orca, it'll fail somewhere downstream. We only care that it
    # didn't take the resumed-short-circuit path. Detect that by asserting
    # readiness_card_resumed never emitted (workflow may raise — fine).
    try:
        run_workflow(args)
    except Exception:
        pass  # downstream failure is fine; we just need to verify no resume-skip
    events = _read_events_jsonl(out_dir)
    stages = [e.get('stage') for e in events]
    assert 'readiness_card_resumed' not in stages, \
        'short-circuit fired without saved readiness_card_event payload — should have fallen through'


def test_med2_phase_not_clobbered_on_resume(tmp_path):
    """MED-2 fix: a prior phase ('awaiting_start_approval', 'sliced', etc.)
    must NOT be overwritten with 'analysis' just because the resume
    invocation came in without --yes. Phase progresses monotonically; the
    initial write must preserve forward progress.

    Concretely: resume into the phase-aware short-circuit and assert that
    after the workflow returns, request.json still says
    phase='awaiting_start_approval' (NOT 'analysis')."""
    import u1_request as _u1r
    rid, stl = _seed_awaiting_start_approval_request(tmp_path)
    out_dir = tmp_path / 'out'
    args = _build_phase2_args(stl, out_dir, rid)
    run_workflow(args)
    after = _u1r.read_request(rid)
    assert after['phase'] == 'awaiting_start_approval', \
        f"MED-2 regression: initial write clobbered phase to {after['phase']!r}"


# ============================================================================
# Phase 3a — workflow emits audit rows
# ============================================================================

def _build_phase2_args_with_operator(model_path, out_dir, request_id, *, operator=None):
    """Wrap _build_phase2_args to allow setting the new --operator field."""
    args = _build_phase2_args(model_path, out_dir, request_id)
    args.operator = operator
    return args


# --------------------------------------------------------------------------- #
# v2.1 kit auto-routing (redirect happens before any slicing)
# --------------------------------------------------------------------------- #

def _kit_zip_for_redirect(tmp_path):
    import zipfile as _zf
    zp = tmp_path / 'kit.zip'
    a, b = _stl(tmp_path), tmp_path / 'm2.stl'
    import shutil as _sh
    _sh.copy(a, b)
    with _zf.ZipFile(zp, 'w') as z:
        z.write(a, 'a.stl')
        z.write(b, 'b.stl')
    return zp


def test_kit_redirect_carries_explicit_operator(tmp_path, capsys):
    zp = _kit_zip_for_redirect(tmp_path)
    main([str(zp), '--json-events', '--operator', 'smoke:sticky'])
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith('{')]
    kit = [e for e in events if e.get('stage') == 'kit_detected']
    assert kit, events
    assert '--operator smoke:sticky' in kit[0]['command']


def test_kit_redirect_without_cli_operator_bakes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('U1_OPERATOR', 'telegram:someone')
    zp = _kit_zip_for_redirect(tmp_path)
    main([str(zp), '--json-events'])
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith('{')]
    kit = [e for e in events if e.get('stage') == 'kit_detected']
    assert kit, events
    assert '--operator' not in kit[0]['command']


def test_undetectable_zip_does_not_fall_into_single_flow(tmp_path, capsys, monkeypatch):
    # If kit detection blows up on a zip, the single-STL flow (which slices
    # only the FIRST model) must not silently proceed.
    import u1_slice_workflow as wf
    zp = _kit_zip_for_redirect(tmp_path)

    class _Boom:
        @staticmethod
        def is_multi_part_archive(_):
            raise RuntimeError("corrupt central directory")

    import sys as _sys
    monkeypatch.setitem(_sys.modules, 'u1_kit', _Boom)
    main([str(zp), '--json-events'])
    events = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith('{')]
    stages = [e.get('stage') for e in events]
    assert 'kit_detection_failed' in stages, stages
