from pathlib import Path
import u1_print_start_gate as g

def idle(): return {'print_stats':{'state':'standby'}, 'virtual_sdcard':{'is_active':False}, 'pause_resume':{'is_paused':False}, 'toolhead':{'extruder':'extruder1'}}

def test_preflight_failure_aborts_before_camera(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h,p: {'print_stats':{'state':'printing'}, 'virtual_sdcard':{'is_active':True}, 'pause_resume':{'is_paused':False}})
    called={'camera':False}; monkeypatch.setattr(g, 'capture_snapshot', lambda out: called.__setitem__('camera', True))
    res=g.run_gate('x.gcode', host='h', port=1, out_dir=tmp_path)
    assert not res['started'] and res['blockers'] and not called['camera']

def test_cancel_bed_clear_never_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h,p: idle())
    called={'start':False}
    res=g.run_gate('x.gcode', bed_clear='cancel', host='h', port=1, intended_tool='extruder1', out_dir=tmp_path, start_func=lambda *a: called.__setitem__('start', True))
    assert res['cancelled'] and not called['start'] and Path(res['snapshot']).exists()

def test_start_only_after_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(g, 'query_state', lambda h,p: idle())
    res=g.run_gate('x.gcode', bed_clear='start', host='h', port=1, intended_tool='extruder1', out_dir=tmp_path, start_func=lambda *a: {'ok':True})
    assert res['started'] is True
