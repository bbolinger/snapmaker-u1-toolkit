#!/usr/bin/env python3
from __future__ import annotations
import argparse, colorsys, json, urllib.request
from pathlib import Path
from typing import Any
from u1_config import get_u1_host, get_u1_port
TOOLS=[('T0','extruder',1),('T1','extruder1',2),('T2','extruder2',3),('T3','extruder3',4)]
# Name by hue, not by nearest RGB swatch: the printer's dark shades (#E65100
# deep orange, #1B5E20 dark green) sat closer to the darker red swatch than to
# the bright orange one, so an orange spool read as "red" on the head screen.
# Upper hue bound (degrees) -> name; lightness and saturation decide
# white/silver/gray/black, beige, brown and pink below.
_HUE_NAMES=((11,'red'),(42,'orange'),(70,'yellow'),(160,'green'),(200,'cyan'),(245,'blue'),(300,'purple'),(345,'pink'),(361,'red'))
def rgba_to_color_name(rgba: Any) -> str:
    if not isinstance(rgba, str) or not rgba: return 'unknown'
    h=rgba.strip().lstrip('#').upper()
    if len(h) not in (6,8) or any(c not in '0123456789ABCDEF' for c in h): return rgba
    r,g,b=int(h[0:2],16)/255,int(h[2:4],16)/255,int(h[4:6],16)/255
    hue,light,_=colorsys.rgb_to_hls(r,g,b)
    hi=max(r,g,b); chroma=hi-min(r,g,b); sat=chroma/hi if hi else 0.0
    if hi<0.15: return 'black'
    if chroma<0.2 and sat<0.35:
        if chroma>=0.02 and r>=g>=b and light>0.7: return 'beige'
        return 'white' if hi>0.92 else 'silver' if hi>0.65 else 'gray' if hi>0.2 else 'black'
    name=next(n for top,n in _HUE_NAMES if hue*360<top)
    if hi<0.65 and (name=='orange' or (name=='red' and sat<0.6)): return 'brown'
    if name in ('orange','yellow') and light>0.8 and chroma<0.3: return 'beige'
    if name=='red' and light>0.7: return 'pink'
    return name
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
        color_rgba=_get(ptc.get('filament_color_rgba'), i, 'unknown') or 'unknown'
        color_name=rgba_to_color_name(color_rgba)
        label=f'{tool}: {vendor} {color_name} {material} (loaded)'
        opts.append({'label':label,'value':tool,'object':obj,'printhead':ph,'material':material,'vendor':vendor,'color_rgba':color_rgba,'color_name':color_name,'loaded':True})
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
