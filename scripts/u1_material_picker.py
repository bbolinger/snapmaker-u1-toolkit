#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, urllib.request
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port
TOOLS=[('T0','extruder',1),('T1','extruder1',2),('T2','extruder2',3),('T3','extruder3',4)]
def http_json(url: str, timeout: float=8.0)->dict[str,Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r: return json.loads(r.read().decode())
def _get(v, i, default=None): return v[i] if isinstance(v, list) and i < len(v) else default
def status_to_options(status: dict[str,Any], requested_material: str|None=None) -> list[dict[str,Any]]:
    ptc=status.get('print_task_config',{}); fd=status.get('filament_detect',{}).get('info',[])
    opts=[]
    for i,(tool,obj,ph) in enumerate(TOOLS):
        exists=_get(ptc.get('filament_exist'), i)
        sensor_loaded=None
        if isinstance(fd,list) and i < len(fd) and isinstance(fd[i],dict):
            sensor_loaded=fd[i].get('FILAMENT_EXIST') or fd[i].get('filament_exist') or fd[i].get('detected')
        loaded = bool(exists) if exists is not None else bool(sensor_loaded)
        if not loaded: continue
        material=_get(ptc.get('filament_type'), i, 'unknown') or 'unknown'
        vendor=_get(ptc.get('filament_vendor'), i, 'unknown') or 'unknown'
        color=_get(ptc.get('filament_color_rgba'), i, 'unknown') or 'unknown'
        label=f'{tool}: {vendor} {color} {material} (loaded)'
        opts.append({'label':label,'value':tool,'object':obj,'printhead':ph,'material':material,'vendor':vendor,'color_rgba':color,'loaded':True})
    if opts:
        req=(requested_material or '').lower()
        preferred=next((o for o in opts if req and req in str(o.get('material','')).lower()), opts[0])
        preferred['recommended']=True
    return opts
def query_material_options(host=None, port=None, requested_material=None):
    host=host or get_u1_host(); port=port or get_u1_port()
    q='print_task_config&filament_detect'
    status=http_json(f'http://{host}:{port}/printer/objects/query?{q}')['result']['status']
    return status_to_options(status, requested_material)
def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--material'); ap.add_argument('--json', action='store_true'); a=ap.parse_args(argv)
    opts=query_material_options(requested_material=a.material)
    print(json.dumps(opts, indent=2) if a.json else '\n'.join(o['label'] for o in opts)); return 0
if __name__=='__main__': raise SystemExit(main())
