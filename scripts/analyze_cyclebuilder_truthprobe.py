#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_cyclebuilder_truthprobe.py — CycleBuilder Diagnose Tool

S02.CycleBuilder-TruthProbe v0.2

Analyseert jsonl logs met l2_debug en geeft één van deze diagnoses:
- POOL_OUT_OF_RANGE: to_pool vaak 3 of None (bit-extract fout)
- POOL_STUCK: to_pool verandert nauwelijks
- POOL_SET_INCOMPLETE: {0,1,2} nooit compleet per sensor
- CYCLES_NEVER_EMIT: pools ok maar cycles_emitted_n altijd 0
- CYCLES_EMIT_BUT_TOTAL_STUCK: emit>0 maar total_cycles_physical=0
- OK_CYCLES_PRESENT: cycles werken correct

Gebruik:
    python3 analyze_cyclebuilder_truthprobe.py live_encoder_*_debug.jsonl
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from typing import List, Dict, Any, Optional, Tuple


# === Diagnose Codes ===

DIAG_POOL_OUT_OF_RANGE = "POOL_OUT_OF_RANGE"
DIAG_POOL_STUCK = "POOL_STUCK"
DIAG_POOL_SET_INCOMPLETE = "POOL_SET_INCOMPLETE"
DIAG_CYCLES_NEVER_EMIT = "CYCLES_NEVER_EMIT"
DIAG_CYCLES_EMIT_BUT_TOTAL_STUCK = "CYCLES_EMIT_BUT_TOTAL_STUCK"
DIAG_OK_CYCLES_PRESENT = "OK_CYCLES_PRESENT"
DIAG_NO_DEBUG_DATA = "NO_DEBUG_DATA"
DIAG_NO_EVENTS = "NO_EVENTS"


def load_log(filepath: str) -> List[Dict[str, Any]]:
    """Load JSONL log file."""
    records = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def analyze_truthprobe(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyze l2_debug data and compute diagnostic metrics.
    
    Patch D: Parse l2_debug and compute stats.
    """
    results = {
        "n_records": len(records),
        "n_records_with_debug": 0,
        "n_records_with_events": 0,
        "duration_s": 0.0,
        
        # Pool stats
        "to_pool_histogram": Counter(),
        "to_pool_per_sensor": {"0": Counter(), "1": Counter()},
        "to_pool_valid_pct": 0.0,        # % in {0,1,2}
        "to_pool_out_of_range_pct": 0.0, # % == 3 or >2
        "to_pool_missing_pct": 0.0,      # % None
        
        # Pool changes
        "pool_changes_total": 0,
        "pool_changes_per_sec": 0.0,
        
        # Unique set hits
        "unique_set_hits_A": 0,
        "unique_set_hits_B": 0,
        "unique_set_hit_rate_A": 0.0,
        "unique_set_hit_rate_B": 0.0,
        
        # Cycles emission
        "cycles_emitted_total": 0,
        "cycles_emitted_any_count": 0,  # records with cycles_emitted_n > 0
        "cycles_emitted_any_rate": 0.0,
        
        # Final totals
        "total_cycles_physical_final": 0.0,
        "cycles_state_counts_final": {"A": 0, "B": 0},
        
        # First cycle info
        "first_cycle_t_abs_s": None,
        "first_cycle_record_idx": None,
        
        # Raw samples for debugging
        "sample_pools_A": [],
        "sample_pools_B": [],
        "sample_to_pool_sequence": [],
    }
    
    if not records:
        return results
    
    # Duration
    t_start = records[0].get("t_abs_s", 0)
    t_end = records[-1].get("t_abs_s", 0)
    results["duration_s"] = t_end - t_start
    
    # Tracking
    prev_to_pool = None
    
    for i, rec in enumerate(records):
        events_batch = rec.get("events_this_batch", 0)
        if events_batch > 0:
            results["n_records_with_events"] += 1
        
        l2_debug = rec.get("l2_debug")
        if l2_debug is None:
            continue
        
        results["n_records_with_debug"] += 1
        
        # === D1: Parse l2_debug fields ===
        ev_sensor = l2_debug.get("ev_sensor")
        ev_to_pool = l2_debug.get("ev_to_pool")
        pools_recent_A = l2_debug.get("pools_recent_A", [])
        pools_recent_B = l2_debug.get("pools_recent_B", [])
        unique_pools_A = set(l2_debug.get("unique_pools_A", []))
        unique_pools_B = set(l2_debug.get("unique_pools_B", []))
        cycles_emitted_n = l2_debug.get("cycles_emitted_n", 0)
        total_cycles_physical = l2_debug.get("total_cycles_physical_after", 
                                             l2_debug.get("total_cycles_physical", 0))
        cycles_state_counts = l2_debug.get("cycles_state_counts", {})
        
        # === D2: Compute stats ===
        
        # to_pool histogram
        if ev_to_pool is not None:
            results["to_pool_histogram"][ev_to_pool] += 1
            if ev_sensor is not None:
                results["to_pool_per_sensor"][str(ev_sensor)][ev_to_pool] += 1
            
            # Track changes
            if prev_to_pool is not None and ev_to_pool != prev_to_pool:
                results["pool_changes_total"] += 1
            prev_to_pool = ev_to_pool
            
            # Sample sequence (first 50)
            if len(results["sample_to_pool_sequence"]) < 50:
                results["sample_to_pool_sequence"].append({
                    "idx": i, "sensor": ev_sensor, "to_pool": ev_to_pool
                })
        
        # Unique set hits ({0,1,2} complete)
        if unique_pools_A == {0, 1, 2}:
            results["unique_set_hits_A"] += 1
        if unique_pools_B == {0, 1, 2}:
            results["unique_set_hits_B"] += 1
        
        # Sample pools (first few)
        if len(results["sample_pools_A"]) < 20 and pools_recent_A:
            results["sample_pools_A"].append({"idx": i, "pools": pools_recent_A})
        if len(results["sample_pools_B"]) < 20 and pools_recent_B:
            results["sample_pools_B"].append({"idx": i, "pools": pools_recent_B})
        
        # Cycles emission
        if cycles_emitted_n > 0:
            results["cycles_emitted_total"] += cycles_emitted_n
            results["cycles_emitted_any_count"] += 1
            
            # First cycle
            if results["first_cycle_t_abs_s"] is None:
                results["first_cycle_t_abs_s"] = rec.get("t_abs_s")
                results["first_cycle_record_idx"] = i
        
        # Update final totals
        results["total_cycles_physical_final"] = total_cycles_physical
        if cycles_state_counts:
            results["cycles_state_counts_final"] = cycles_state_counts
    
    # === Compute derived metrics ===
    n_debug = results["n_records_with_debug"]
    
    if n_debug > 0:
        # to_pool percentages
        hist = results["to_pool_histogram"]
        total_pool_samples = sum(hist.values())
        
        if total_pool_samples > 0:
            valid_count = hist.get(0, 0) + hist.get(1, 0) + hist.get(2, 0)
            out_of_range_count = sum(v for k, v in hist.items() if k not in {0, 1, 2, None})
            
            results["to_pool_valid_pct"] = 100.0 * valid_count / total_pool_samples
            results["to_pool_out_of_range_pct"] = 100.0 * out_of_range_count / total_pool_samples
            results["to_pool_missing_pct"] = 100.0 * (n_debug - total_pool_samples) / n_debug
        
        # Pool changes per sec
        duration = results["duration_s"]
        if duration > 0:
            results["pool_changes_per_sec"] = results["pool_changes_total"] / duration
        
        # Unique set hit rates
        results["unique_set_hit_rate_A"] = 100.0 * results["unique_set_hits_A"] / n_debug
        results["unique_set_hit_rate_B"] = 100.0 * results["unique_set_hits_B"] / n_debug
        
        # Cycles emission rate
        results["cycles_emitted_any_rate"] = 100.0 * results["cycles_emitted_any_count"] / n_debug
    
    return results


def diagnose(stats: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    """
    Apply heuristic diagnosis rules (Patch E).
    
    Returns: (diagnosis_code, explanation, next_actions)
    """
    # No data cases
    if stats["n_records"] == 0:
        return DIAG_NO_EVENTS, "No records in log file", ["Check log file is not empty"]
    
    if stats["n_records_with_debug"] == 0:
        return DIAG_NO_DEBUG_DATA, "No l2_debug data found in log", [
            "Run with S02_DEBUG_CYCLES=1 environment variable",
            "Verify realtime_states_v1_9_canonical.py has debug patches"
        ]
    
    # === E1: POOL_OUT_OF_RANGE ===
    if stats["to_pool_out_of_range_pct"] > 5.0 or stats["to_pool_missing_pct"] > 5.0:
        return DIAG_POOL_OUT_OF_RANGE, (
            f"to_pool out of range: {stats['to_pool_out_of_range_pct']:.1f}% invalid, "
            f"{stats['to_pool_missing_pct']:.1f}% missing"
        ), [
            "Verify parse_event24() bit extraction: to_pool = (flags1 >> 4) & 0x3",
            "Check firmware flags layout matches expected format",
            "Compare flags1 hex values with expected pool encoding"
        ]
    
    # === E2: POOL_STUCK ===
    n_events = stats["n_records_with_events"]
    if stats["pool_changes_per_sec"] < 0.2 and n_events > 50:
        return DIAG_POOL_STUCK, (
            f"Pool changes too slow: {stats['pool_changes_per_sec']:.2f}/sec "
            f"with {n_events} events"
        ), [
            "to_pool appears constant - sensor may be stuck or decode wrong",
            "Verify sensor is physically active (LED blinks, etc)",
            "Check if sensor index mapping is correct (0=A, 1=B)"
        ]
    
    # === E3: POOL_SET_INCOMPLETE ===
    hit_rate_A = stats["unique_set_hit_rate_A"]
    hit_rate_B = stats["unique_set_hit_rate_B"]
    valid_pct = stats["to_pool_valid_pct"]
    
    if hit_rate_A < 1.0 and hit_rate_B < 1.0 and valid_pct > 90.0:
        return DIAG_POOL_SET_INCOMPLETE, (
            f"Pool set {{0,1,2}} never complete: A={hit_rate_A:.1f}%, B={hit_rate_B:.1f}% "
            f"(but to_pool valid={valid_pct:.1f}%)"
        ), [
            "Pools are valid but never form {NEU, N, S} = {0, 1, 2} per sensor",
            "Check neutral/N/S pool mapping matches hardware",
            "Verify 3-point window is seeing all three pool states",
            f"Sample pools A: {stats.get('sample_pools_A', [])[:5]}",
            f"Sample pools B: {stats.get('sample_pools_B', [])[:5]}"
        ]
    
    # === E4: CYCLES_NEVER_EMIT ===
    emit_rate = stats["cycles_emitted_any_rate"]
    combined_hit_rate = max(hit_rate_A, hit_rate_B)
    
    if combined_hit_rate > 10.0 and emit_rate == 0:
        return DIAG_CYCLES_NEVER_EMIT, (
            f"Pool set complete {combined_hit_rate:.1f}% but cycles_emitted=0"
        ), [
            "CyclesState gate/order check is blocking cycle detection",
            "Expected order: [N, NEU, S]=[1,0,2] for cycle_up, [S, NEU, N]=[2,0,1] for cycle_down",
            "Check if pool sequence matches expected cycle patterns",
            f"Sample sequence: {stats.get('sample_to_pool_sequence', [])[:10]}"
        ]
    
    # === E5: CYCLES_EMIT_BUT_TOTAL_STUCK ===
    total_cycles = stats["total_cycles_physical_final"]
    cycles_emitted = stats["cycles_emitted_total"]
    
    if cycles_emitted > 0 and total_cycles == 0:
        return DIAG_CYCLES_EMIT_BUT_TOTAL_STUCK, (
            f"Cycles emitted ({cycles_emitted}) but total_cycles_physical=0"
        ), [
            "Accumulator/claim path issue in TilesState or RealtimePipeline",
            "Check tile aggregation: cycles may emit but not aggregate to tiles",
            "Verify movement_state dict key: 'total_cycles_physical'"
        ]
    
    # === E6: OK ===
    if total_cycles > 0:
        first_t = stats["first_cycle_t_abs_s"]
        first_idx = stats["first_cycle_record_idx"]
        cyc_counts = stats["cycles_state_counts_final"]
        
        return DIAG_OK_CYCLES_PRESENT, (
            f"Cycles working: {total_cycles:.1f} total, "
            f"first at t={first_t:.2f}s (record #{first_idx}), "
            f"per sensor: A={cyc_counts.get('A', 0)}, B={cyc_counts.get('B', 0)}"
        ), [
            "Cycle detection is functioning correctly",
            "If displacement still 0, check TilesState → L1 integration"
        ]
    
    # Fallback
    return "UNKNOWN", "Could not determine diagnosis", [
        "Review raw stats and log data manually"
    ]


def print_diagnosis(filepath: str, stats: Dict[str, Any], 
                    diagnosis: str, explanation: str, actions: List[str]):
    """Print formatted diagnosis output (Patch F)."""
    
    print()
    print("=" * 70)
    print(f"  CYCLEBUILDER TRUTHPROBE: {Path(filepath).name}")
    print("=" * 70)
    print()
    
    # Key metrics
    print(f"  Records:           {stats['n_records']} total, {stats['n_records_with_debug']} with debug")
    print(f"  Duration:          {stats['duration_s']:.1f}s")
    print(f"  Events:            {stats['n_records_with_events']} records with events")
    print()
    
    # Pool stats
    print("─" * 70)
    print("  POOL STATISTICS")
    print("─" * 70)
    hist = stats["to_pool_histogram"]
    total = sum(hist.values())
    if total > 0:
        for pool in sorted(hist.keys()):
            count = hist[pool]
            pct = 100.0 * count / total
            label = {0: "NEU", 1: "N", 2: "S"}.get(pool, f"?{pool}")
            bar = "█" * int(pct / 5)
            print(f"  pool={pool} ({label:3}): {count:6} ({pct:5.1f}%) {bar}")
    else:
        print("  No pool data")
    print()
    print(f"  Valid (0,1,2):     {stats['to_pool_valid_pct']:.1f}%")
    print(f"  Out of range:      {stats['to_pool_out_of_range_pct']:.1f}%")
    print(f"  Pool changes/sec:  {stats['pool_changes_per_sec']:.2f}")
    print()
    
    # Unique set hits
    print("─" * 70)
    print("  UNIQUE SET {{0,1,2}} HIT RATE")
    print("─" * 70)
    print(f"  Sensor A:          {stats['unique_set_hit_rate_A']:.1f}% ({stats['unique_set_hits_A']} hits)")
    print(f"  Sensor B:          {stats['unique_set_hit_rate_B']:.1f}% ({stats['unique_set_hits_B']} hits)")
    print()
    
    # Cycles
    print("─" * 70)
    print("  CYCLE EMISSION")
    print("─" * 70)
    print(f"  Emitted total:     {stats['cycles_emitted_total']}")
    print(f"  Emit rate:         {stats['cycles_emitted_any_rate']:.1f}% of debug records")
    print(f"  Final total_cy:    {stats['total_cycles_physical_final']:.1f}")
    cyc_counts = stats["cycles_state_counts_final"]
    print(f"  Per sensor:        A={cyc_counts.get('A', 0)}, B={cyc_counts.get('B', 0)}")
    
    if stats["first_cycle_t_abs_s"] is not None:
        print(f"  First cycle:       t={stats['first_cycle_t_abs_s']:.2f}s (record #{stats['first_cycle_record_idx']})")
    print()
    
    # === DIAGNOSIS ===
    print("=" * 70)
    color_map = {
        DIAG_OK_CYCLES_PRESENT: "\033[92m",  # Green
        DIAG_NO_DEBUG_DATA: "\033[93m",      # Yellow
        DIAG_NO_EVENTS: "\033[93m",          # Yellow
    }
    color = color_map.get(diagnosis, "\033[91m")  # Red for errors
    reset = "\033[0m"
    
    print(f"  {color}DIAGNOSIS: {diagnosis}{reset}")
    print()
    print(f"  {explanation}")
    print()
    print("  NEXT ACTIONS:")
    for i, action in enumerate(actions, 1):
        print(f"    {i}. {action}")
    print()
    print("=" * 70)
    print()


def main():
    parser = argparse.ArgumentParser(
        description='CycleBuilder TruthProbe Diagnosis Tool'
    )
    parser.add_argument('files', nargs='+', help='JSONL log files with l2_debug')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--summary', action='store_true', help='One-line summary per file')
    
    args = parser.parse_args()
    
    for filepath in args.files:
        if not Path(filepath).exists():
            print(f"❌ File not found: {filepath}")
            continue
        
        records = load_log(filepath)
        stats = analyze_truthprobe(records)
        diagnosis, explanation, actions = diagnose(stats)
        
        if args.json:
            output = {
                "file": filepath,
                "stats": stats,
                "diagnosis": diagnosis,
                "explanation": explanation,
                "actions": actions,
            }
            # Convert Counter to dict for JSON
            output["stats"]["to_pool_histogram"] = dict(output["stats"]["to_pool_histogram"])
            output["stats"]["to_pool_per_sensor"] = {
                k: dict(v) for k, v in output["stats"]["to_pool_per_sensor"].items()
            }
            print(json.dumps(output, indent=2))
        
        elif args.summary:
            total_cy = stats["total_cycles_physical_final"]
            emit_rate = stats["cycles_emitted_any_rate"]
            valid_pct = stats["to_pool_valid_pct"]
            print(f"{Path(filepath).name}: {diagnosis} | "
                  f"cy={total_cy:.0f} emit={emit_rate:.1f}% valid_pool={valid_pct:.1f}%")
        
        else:
            print_diagnosis(filepath, stats, diagnosis, explanation, actions)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
