#!/bin/bash
# v1.6 draft-profile validation experiment v2 — no set -e, drop v3.
ORCA=/opt/data/tools/orcaslicer/squashfs-root/AppRun
LD=/opt/data/tools/orcaslicer/local-libs/usr/lib/x86_64-linux-gnu:/opt/data/tools/orcaslicer/squashfs-root/usr/lib
export LD_LIBRARY_PATH=$LD
MACHINE="/opt/data/tools/orcaslicer/squashfs-root/resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4 nozzle).json"
FILAMENT="/opt/data/artifacts/slice_workflow/wall_mount_laid_on_back/20260626-123427/Snapmaker PETG @U1__flat.json"
PROD_PROC="/opt/data/artifacts/slice_workflow/wall_mount_laid_on_back/20260626-123427/0.20 Strength @Snapmaker U1 (0.4 nozzle)__no_supports.json"

WORK=/opt/data/snapmaker_u1/v16-experiment2
rm -rf $WORK; mkdir -p $WORK/profiles $WORK/runs
RESULTS=$WORK/results.tsv
echo -e "fixture\tvariant\twall_clock_ms\trc\twarning_category\twarning_text\toverhang_pct\tlayer_count" > $RESULTS

# === Build variants ===
build_variant() {
    cp "$PROD_PROC" $WORK/profiles/$1.json
    python3 -c "
import json
p='$WORK/profiles/$1.json'
d=json.load(open(p))
d['layer_height']='$2'
d['initial_layer_print_height']='$2'
d['wall_loops']='$3'
d['sparse_infill_density']='$4%'
d['top_shell_layers']='$5'
d['bottom_shell_layers']='$5'
d['gcode_thumbnails']='0'
d['enable_support']='0'
json.dump(d, open(p,'w'))
"
}
# name layer walls infill top_bottom_layers
build_variant ground 0.20 6 25 4   # production-equiv: GROUND TRUTH
build_variant v1     0.30 1 0  0    # proposed default
build_variant v2     0.20 1 0  0    # production layer / minimal
build_variant v4     0.30 2 0  0    # +1 wall
build_variant v5     0.30 1 0  1    # +1 top-bottom
# (v3 0.4mm dropped — too thick for 0.4 nozzle)

# === Categorize warning ===
categorize() {
    local txt="$1"
    case "$txt" in
        ""|"null") echo CLEAN ;;
        *floating\ cantilever*|*loating\ cantilever*) echo CANTILEVER ;;
        *floating\ region*|*loating\ region*) echo FLOATING_REGIONS ;;
        *verhang*) echo OVERHANG_FLAGGED ;;
        *) echo UNKNOWN ;;
    esac
}

# === One trial — never fails the script ===
trial() {
    local fixture_name=$1; local stl_path=$2; local variant=$3
    local OUT=$WORK/runs/${fixture_name}-${variant}
    mkdir -p $OUT
    local T0=$(date +%s%N)
    timeout 60 "$ORCA" \
        --load-settings "$MACHINE;$WORK/profiles/${variant}.json" \
        --load-filaments "$FILAMENT" \
        --outputdir $OUT \
        --slice 0 \
        --mstpp 30 \
        "$stl_path" >/dev/null 2>&1
    local _ignored_rc=$?
    local T1=$(date +%s%N)
    local MS=$(( (T1-T0)/1000000 ))

    local RC=missing; local WARN_TXT=""; local CAT=NO_RESULT
    if [[ -f $OUT/result.json ]]; then
        RC=$(python3 -c "import json; print(json.load(open('$OUT/result.json')).get('return_code','?'))" 2>/dev/null)
        WARN_TXT=$(python3 -c "import json; d=json.load(open('$OUT/result.json')); p=d.get('sliced_plates',[{}])[0]; print(p.get('warning_message','').replace(chr(9),' ').replace(chr(10),' '))" 2>/dev/null)
        CAT=$(categorize "$WARN_TXT")
    fi
    local OVR_PCT=NA; local LAYERS=NA
    if [[ -f $OUT/plate_1.gcode ]]; then
        IFS=' ' read OVR_PCT LAYERS <<EOF2
$(python3 -c "
gc='$OUT/plate_1.gcode'
cur=0; with_ov=set()
with open(gc) as f:
    for line in f:
        if line.startswith(';LAYER_CHANGE'): cur+=1
        if line.startswith(';TYPE:Overhang wall'): with_ov.add(cur)
pct = 100*len(with_ov)/max(1,cur)
print(f'{pct:.1f} {cur}')
" 2>/dev/null)
EOF2
    fi
    echo -e "${fixture_name}\t${variant}\t${MS}\t${RC}\t${CAT}\t${WARN_TXT}\t${OVR_PCT}\t${LAYERS}" >> $RESULTS
    echo "  ${fixture_name}-${variant}: ${MS}ms rc=$RC cat=$CAT ovr=${OVR_PCT}% layers=$LAYERS"
}

declare -A FIXTURES=(
    [wall_mount_source]=/opt/data/artifacts/slice_workflow/wall_mount_laid_on_back/20260626-123427/source.stl
    [wall_mount_auto]=/opt/data/artifacts/slice_workflow/CaulkHolderWallMount/20260626-142113/auto_oriented.stl
    [caulkholder_source]=/opt/data/artifacts/slice_workflow/CaulkHolderWallMount/20260626-142113/source.stl
    [benchy]=/opt/data/snapmaker_u1/fixtures-v16/benchy.stl
    [overhang_fan]=/opt/data/snapmaker_u1/fixtures-v16/overhang_fan.stl
)

TOTAL_T0=$(date +%s)
for fname in "${!FIXTURES[@]}"; do
    fpath="${FIXTURES[$fname]}"
    [[ ! -f "$fpath" ]] && { echo "MISSING: $fname"; continue; }
    echo "=== $fname ($(stat -c %s $fpath) bytes) ==="
    for v in ground v1 v2 v4 v5; do
        trial $fname "$fpath" $v
    done
done
echo "==================================="
echo "Total wall-clock: $(( $(date +%s) - TOTAL_T0 ))s"
echo "Results: $RESULTS"
