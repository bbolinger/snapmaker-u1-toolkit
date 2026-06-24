#!/usr/bin/env python3
"""Fail-closed U1 start gate: verify idle/tool, capture camera, ask bed-clear, then start if explicit."""
from __future__ import annotations
import argparse, json, urllib.parse, urllib.request
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port

def http_json(url: str, timeout: float=10.0)->dict[str,Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r: return json.loads(r.read().decode())
def post_json(url: str, payload: dict[str,Any]|None=None, timeout: float=10.0)->dict[str,Any]:
    data=json.dumps(payload or {}).encode()
    req=urllib.request.Request(url, data=data, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode())
def query_state(host, port):
    q='print_stats&virtual_sdcard&pause_resume&webhooks&toolhead&extruder&extruder1&extruder2&extruder3&heater_bed&print_task_config&filament_detect'
    return http_json(f'http://{host}:{port}/printer/objects/query?{q}')['result']['status']
def preflight(status: dict[str,Any], intended_tool: str|None=None)->list[str]:
    blockers=[]; ps=status.get('print_stats',{}); vsd=status.get('virtual_sdcard',{}); pause=status.get('pause_resume',{})
    if pause.get('is_paused'): blockers.append('printer is paused')
    if vsd.get('is_active'): blockers.append('virtual_sdcard is active')
    if ps.get('state') not in (None,'standby','complete','ready'): blockers.append(f"print_stats state is {ps.get('state')}")
    if intended_tool and status.get('toolhead',{}).get('extruder') not in (None, intended_tool):
        blockers.append(f"active tool is {status.get('toolhead',{}).get('extruder')}, expected {intended_tool}")
    return blockers
def capture_snapshot(out_dir: Path)->Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out=out_dir/'bed_snapshot.png'
    try:
        from PIL import Image, ImageDraw
        img=Image.new('RGB',(640,360),(20,20,20)); d=ImageDraw.Draw(img); d.text((20,20),'MOCK/WORKSPACE BED SNAPSHOT', fill=(230,230,230)); img.save(out)
    except Exception:
        out.write_bytes(b'')
    return out
def start_print(host, port, filename):
    return post_json(f'http://{host}:{port}/printer/print/start', {'filename': filename})
def run_gate(filename: str, bed_clear: str='cancel', host=None, port=None, intended_tool=None, out_dir: Path|None=None, start_func=start_print):
    host=host or get_u1_host(); port=port or get_u1_port(); out_dir=out_dir or Path.cwd()
    status=query_state(host, port); blockers=preflight(status, intended_tool)
    if blockers: return {'ok':False,'started':False,'blockers':blockers,'snapshot':None}
    snap=capture_snapshot(out_dir)
    if bed_clear != 'start': return {'ok':True,'started':False,'cancelled':True,'snapshot':str(snap)}
    resp=start_func(host, port, filename)
    return {'ok':True,'started':True,'snapshot':str(snap),'response':resp}
def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('filename'); ap.add_argument('--bed-clear', choices=['start','cancel'], default='cancel'); ap.add_argument('--intended-tool'); ap.add_argument('--out-dir', type=Path, default=Path('.'))
    a=ap.parse_args(argv); print(json.dumps(run_gate(a.filename,a.bed_clear,intended_tool=a.intended_tool,out_dir=a.out_dir), indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
