#!/usr/bin/env python3
"""
analyze_handencoder_log.py — V4.7b Canonical Cycle Truth Analyzer

Analyzes JSONL logs from live_symphonia for:
- MDI modes A/B/C performance
- Canonical cycle truth (total_cycles_physical from v1.9)
- Origin readiness diagnosis

Changelog v0.4.7b:
- Based on v1.9 canonical analysis
- total_cycles_physical is the canonical source
"""
import json, sys
from pathlib import Path
from collections import Counter
INF = float('inf')

def load_jsonl(p):
    with open(p) as f: return [json.loads(l) for l in f if l.strip()]

def analyze(records):
    if not records: return {"error": "empty"}
    n = len(records)
    dur = records[-1].get("t_s", 0) - records[0].get("t_s", 0) if n > 1 else 0
    total_ev = sum(r.get("ev", 0) for r in records)
    
    # Timeline
    first_pre_mov = first_cand = first_comm = first_mov = first_micro_t0 = None
    latch_count = latch_dropped = latch_confirmed = 0
    aw_states, aw_reasons = Counter(), Counter()
    ev_wins = []
    max_mdi_disp = 0
    mdi_mode = "C"
    
    # Canonical cycle truth
    final_total_cycles = 0.0
    final_cb_total = 0
    max_total_cycles = 0.0
    max_cb_total = 0
    mismatches = 0
    cycles_source_key = "total_cycles_physical"
    
    for r in records:
        t = r.get("t_s", 0)
        aw = r.get("aw", {})
        state, reason = aw.get("state", "?"), aw.get("reason", "?")
        aw_states[state] += 1
        aw_reasons[reason] += 1
        
        mdi = r.get("mdi", {})
        if "mode" in mdi: mdi_mode = mdi["mode"]
        ev_win = mdi.get("ev_win", 0)
        if ev_win > 0: ev_wins.append(ev_win)
        mdi_disp = mdi.get("disp_deg", 0)
        if mdi_disp > max_mdi_disp: max_mdi_disp = mdi_disp
        mt0 = mdi.get("t0")
        if mt0 and first_micro_t0 is None: first_micro_t0 = mt0
        
        if mdi.get("latch_set") and mdi.get("confirmed"): latch_confirmed += 1
        if reason == "MDI_LATCH": latch_count += 1
        if reason == "MDI_LATCH_DROPPED": latch_dropped += 1
        
        if state == "PRE_MOVEMENT" and first_pre_mov is None: first_pre_mov = t
        cand = r.get("candidate", {})
        comm = r.get("commit", {})
        if cand.get("set") and first_cand is None: first_cand = t
        if comm.get("set") and first_comm is None: first_comm = t
        if state == "MOVEMENT" and first_mov is None: first_mov = t
        
        # Cycle truth
        ct = r.get("_cycle_truth", {})
        if ct:
            used = ct.get("used_total", 0)
            cb = ct.get("cb_total", 0)
            final_total_cycles = used
            final_cb_total = cb
            max_total_cycles = max(max_total_cycles, used)
            max_cb_total = max(max_cb_total, cb)
            if ct.get("mismatch"): mismatches += 1
            if ct.get("source_key"): cycles_source_key = ct["source_key"]
        
        # L2 direct
        l2 = r.get("l2", {})
        if l2.get("total_cycles_physical"):
            final_total_cycles = max(final_total_cycles, l2["total_cycles_physical"])
    
    ev_stats = {"min": min(ev_wins), "median": sorted(ev_wins)[len(ev_wins)//2], "max": max(ev_wins)} if ev_wins else {}
    
    return {
        "n_records": n, "duration_s": dur, "total_events": total_ev,
        "mdi_mode": mdi_mode,
        "timeline": {"first_micro_t0_s": first_micro_t0, "first_pre_movement_s": first_pre_mov,
                     "first_candidate_s": first_cand, "first_commit_s": first_comm, "first_movement_s": first_mov},
        "mdi": {"max_disp_deg": max_mdi_disp, "ev_win_stats": ev_stats},
        "latch": {"episodes": latch_count, "dropped": latch_dropped, "confirmed": latch_confirmed},
        "aw_states": dict(aw_states), "aw_reasons": dict(aw_reasons),
        "cycle_truth": {
            "total_cycles_physical_final": final_total_cycles,
            "cb_total_final": final_cb_total,
            "total_cycles_physical_max": max_total_cycles,
            "cb_total_max": max_cb_total,
            "mismatches_logged": mismatches,
            "source_key": cycles_source_key,
        },
    }

def fmt_t(t): return f"{t:.2f}s" if t is not None else "-"

def report(r, path):
    print("=" * 70)
    print(f"  V4.7b CANONICAL CYCLE TRUTH ANALYSIS: {path}")
    print("=" * 70)
    print(f"\n  Records: {r['n_records']}  Duration: {r['duration_s']:.1f}s")
    print(f"  Events: {r['total_events']}")
    print(f"  MDI Mode: {r['mdi_mode']}")
    
    ct = r.get("cycle_truth", {})
    print("\n" + "-" * 70)
    print("  CANONICAL CYCLE TRUTH (v1.9)")
    used = ct.get("total_cycles_physical_final", 0)
    cb = ct.get("cb_total_final", 0)
    src = ct.get("source_key", "total_cycles_physical")
    print(f"  total_cycles_physical: {used}")
    print(f"  CB cycles_total:       {cb}")
    print(f"  Source key:            {src}")
    print(f"  Mismatches logged:     {ct.get('mismatches_logged', 0)}")
    
    if cb > 0 and used == 0:
        if r['total_events'] < 50:
            print(f"\n  ℹ️  Boot phase: {cb} CB cycles, tiles not yet created")
        else:
            print(f"\n  ⚠️  Possible issue: CB={cb} but MovementBody=0")
    elif used > 0:
        print(f"\n  ✅ Cycles flowing to MovementBody")
    else:
        print(f"\n  ℹ️  No cycles (normal for short burst)")
    
    print("\n" + "-" * 70 + "\n  MDI")
    mdi = r.get("mdi", {})
    print(f"  Max micro displacement: {mdi.get('max_disp_deg', 0):.1f}°")
    ev = mdi.get("ev_win_stats", {})
    if ev: print(f"  ev_win: min={ev.get('min')} median={ev.get('median')} max={ev.get('max')}")
    
    lt = r.get("latch", {})
    if r['mdi_mode'] == "C":
        print(f"\n  LATCH: episodes={lt.get('episodes',0)} dropped={lt.get('dropped',0)} confirmed={lt.get('confirmed',0)}")
    
    print("\n" + "-" * 70 + "\n  TIMELINE")
    tl = r["timeline"]
    print(f"  First micro_t0:      {fmt_t(tl.get('first_micro_t0_s'))}")
    print(f"  First PRE_MOVEMENT:  {fmt_t(tl.get('first_pre_movement_s'))}")
    print(f"  First CANDIDATE:     {fmt_t(tl.get('first_candidate_s'))}")
    print(f"  First COMMIT:        {fmt_t(tl.get('first_commit_s'))}")
    print(f"  First MOVEMENT:      {fmt_t(tl.get('first_movement_s'))}")
    
    print("\n" + "=" * 70 + "\n  DIAGNOSIS\n" + "=" * 70)
    issues = []
    pre_mov = r["aw_states"].get("PRE_MOVEMENT", 0) > 0
    if pre_mov: issues.append("✅ Test-1 PASS: PRE_MOVEMENT detected")
    else:
        issues.append("❌ Test-1 FAIL: No PRE_MOVEMENT")
        if mdi.get("max_disp_deg", 0) > 0:
            issues.append(f"   → MDI had displacement ({mdi['max_disp_deg']:.0f}°)")
    if used == 0 and cb == 0:
        issues.append("ℹ️  No cycles (expected for short burst)")
    elif used > 0:
        issues.append(f"✅ Cycles OK: {used:.0f} reached MovementBody")
    for i in issues: print(f"  {i}")
    print("=" * 70)

def main():
    if len(sys.argv) < 2: print("Usage: analyze_handencoder_log.py <file.jsonl>"); return 1
    path = sys.argv[1]
    if not Path(path).exists(): print(f"❌ Not found: {path}"); return 1
    records = load_jsonl(path)
    if not records: print("❌ Empty"); return 1
    result = analyze(records)
    if "--json" in sys.argv: print(json.dumps(result, indent=2, default=str))
    else: report(result, path)
    return 0

if __name__ == "__main__": sys.exit(main())
