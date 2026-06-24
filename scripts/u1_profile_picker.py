#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
PROFILE_DIR=Path(__file__).resolve().parent.parent/'profiles'
def profile_id(path: Path)->str:
    stem=path.stem
    for p in ('community_','merged_'):
        if stem.startswith(p): stem=stem[len(p):]
    return stem.replace('u1_textured_pei','').strip('_')
def list_profiles(profile_dir: Path=PROFILE_DIR, class_hint: str|None=None)->list[dict]:
    files=sorted(profile_dir.glob('*.json'))
    opts=[]
    for p in files:
        pid=profile_id(p)
        label=pid.replace('_',' ')
        opts.append({'label':label,'value':pid,'path':str(p)})
    hint=(class_hint or '').lower()
    def score(o):
        v=o['value'].lower()
        if any(w in hint for w in ['strength','bracket','holder','fixture','utility']) and '020_strength' in v: return 0
        if any(w in hint for w in ['cosmetic','pretty','fine']) and '016_optimal' in v: return 0
        return 1
    if opts:
        best=sorted(opts, key=lambda o:(score(o), o['value']))[0]; best['recommended']=True
    return opts
def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('--class-hint'); ap.add_argument('--json', action='store_true'); a=ap.parse_args(argv)
    opts=list_profiles(class_hint=a.class_hint); print(json.dumps(opts, indent=2) if a.json else '\n'.join(o['label'] for o in opts)); return 0
if __name__=='__main__': raise SystemExit(main())
