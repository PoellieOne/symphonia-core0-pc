#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_handencoder_log.py — Offline analyzer voor handencoder logs

S02.HandEncoder-Observability v2.0.5 — Patch P1/P2/P3/P4

Changelog v2:
- PATCH P1: Signed angles [-180°, +180°) in alle output
- PATCH P2: dt_since_* clamp bij analyse (max = run duration)
- PATCH P3: delta_cycles histogram, scrape-without-displacement score
- PATCH P4: UX: interpretatieregels, betere FIRST DISPLACEMENT output

Gebruik:
    python3 analyze_handencoder_log.py live_encoder_*.jsonl
    python3 analyze_handencoder_log.py *.jsonl --summary
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from typing import List, Dict, Any, Optional


# === PATCH P1: Signed angle helper ===

def wrap_deg_signed(x: float) -> float:
    """Wrap angle to [-180°, +180°)."""
    return ((x + 180.0) % 360.0) - 180.0


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


def analyze_log(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze log records with signed angles and clamped dt's."""
    if not records:
        return {"error": "No records"}
    
    results = {
        "total_records": len(records),
        "duration_s": 0,
        "first_displacement": None,
        "total_cycles": 0,
        "total_events": 0,
        "delta_theta_histogram_signed": defaultdict(int),
        "delta_cycles_histogram": defaultdict(int),
        "top_delta_theta": [],
        "scrape_segments": [],
        "scrape_without_disp_fraction": 0.0,
        "direction_flips": [],
        "state_counts": Counter(),
        "reason_counts": Counter(),
    }
    
    # Duration
    if records:
        t_start = records[0].get("t_abs_s", 0)
        t_end = records[-1].get("t_abs_s", 0)
        results["duration_s"] = t_end - t_start
    
    run_duration = results["duration_s"]
    
    # Trackers
    prev_direction = None
    scrape_start = None
    scrape_max_dtC = 0
    scrape_without_disp_count = 0
    high_activity_count = 0
    
    for i, rec in enumerate(records):
        t = rec.get("t_abs_s", 0)
        l1 = rec.get("l1", {})
        l2 = rec.get("l2", {})
        
        state = l1.get("state", "STILL")
        reason = l1.get("reason", "")
        activity_score = l1.get("activity_score", 0)
        
        # Get delta_theta - prefer signed if available (v2.0.5+)
        if "delta_theta_deg_signed" in rec:
            delta_theta_signed = rec["delta_theta_deg_signed"]
        elif "delta_theta_deg_raw" in rec:
            delta_theta_signed = wrap_deg_signed(rec["delta_theta_deg_raw"])
        else:
            # Fallback to l1 field
            raw = l1.get("delta_theta_deg", 0)
            delta_theta_signed = wrap_deg_signed(raw)
        
        delta_cycles = rec.get("delta_cycles_physical", 0)
        events_batch = rec.get("events_this_batch", 0)
        
        # dt's with clamp (Patch P2)
        dtC = rec.get("dt_since_last_cycle_s", 0)
        dtE = rec.get("dt_since_last_event_s", 0)
        dtC = min(dtC, run_duration) if run_duration > 0 else dtC
        dtE = min(dtE, run_duration) if run_duration > 0 else dtE
        
        direction = l2.get("direction", "UNDECIDED")
        
        # State counts
        results["state_counts"][state] += 1
        results["reason_counts"][reason] += 1
        
        # Total events
        results["total_events"] += events_batch
        
        # First displacement (Patch P1: use signed)
        if results["first_displacement"] is None and abs(delta_theta_signed) > 0.1:
            results["first_displacement"] = {
                "t_abs_s": t,
                "record_index": i,
                "delta_theta_deg_signed": delta_theta_signed,
                "delta_cycles": delta_cycles,
                "dtC": dtC,
                "dtE": dtE,
                "state": state,
                "reason": reason,
            }
        
        # Total cycles
        results["total_cycles"] = rec.get("cycles_physical_total", 0)
        
        # Delta theta histogram (Patch P1: signed, bucket by 15°)
        if abs(delta_theta_signed) > 0.1:
            bucket = round(delta_theta_signed / 15) * 15
            results["delta_theta_histogram_signed"][bucket] += 1
            
            results["top_delta_theta"].append({
                "t_abs_s": t,
                "delta_theta_deg_signed": delta_theta_signed,
                "delta_cycles": delta_cycles,
                "state": state,
            })
        
        # Delta cycles histogram (Patch P3)
        if delta_cycles != 0:
            # Round to 0.5 buckets
            bucket = round(delta_cycles * 2) / 2
            results["delta_cycles_histogram"][bucket] += 1
        
        # Scrape detection
        if state == "SCRAPE":
            if scrape_start is None:
                scrape_start = t
                scrape_max_dtC = dtC
            else:
                scrape_max_dtC = max(scrape_max_dtC, dtC)
        else:
            if scrape_start is not None and scrape_max_dtC > 0.3:
                results["scrape_segments"].append({
                    "t_start": scrape_start,
                    "t_end": t,
                    "duration_s": t - scrape_start,
                    "max_dtC": min(scrape_max_dtC, run_duration),  # Patch P2
                })
            scrape_start = None
            scrape_max_dtC = 0
        
        # Scrape without displacement score (Patch P3)
        if activity_score >= 5.0:  # A1 threshold
            high_activity_count += 1
            if delta_cycles == 0:
                scrape_without_disp_count += 1
        
        # Direction flips
        if direction in ("CW", "CCW"):
            if prev_direction and prev_direction != direction:
                results["direction_flips"].append({
                    "t_abs_s": t,
                    "from": prev_direction,
                    "to": direction,
                })
            prev_direction = direction
    
    # Sort top delta theta by absolute value
    results["top_delta_theta"] = sorted(
        results["top_delta_theta"],
        key=lambda x: abs(x["delta_theta_deg_signed"]),
        reverse=True
    )[:10]
    
    # Scrape without displacement fraction (Patch P3)
    if high_activity_count > 0:
        results["scrape_without_disp_fraction"] = scrape_without_disp_count / high_activity_count
    
    return results


def print_analysis(filepath: str, results: Dict[str, Any]):
    """Print analysis with improved UX (Patch P4)."""
    print()
    print("=" * 70)
    print(f"  HANDENCODER LOG ANALYSIS v2: {Path(filepath).name}")
    print("=" * 70)
    print()
    
    # Summary
    print(f"  Duration:      {results['duration_s']:.1f}s")
    print(f"  Records:       {results['total_records']}")
    print(f"  Total events:  {results['total_events']}")
    print(f"  Total cycles:  {results['total_cycles']:.0f}")
    print()
    
    # === FIRST DISPLACEMENT (Patch P4: enhanced) ===
    print("─" * 70)
    print("  FIRST DISPLACEMENT")
    print("─" * 70)
    fd = results.get("first_displacement")
    if fd:
        print(f"  Time:     t = {fd['t_abs_s']:.2f}s (record #{fd['record_index']})")
        print(f"  Δθ:       {fd['delta_theta_deg_signed']:+.1f}° (SIGNED)")
        print(f"  Δcycles:  {fd['delta_cycles']}")
        print(f"  dtC:      {fd['dtC']:.2f}s  dtE: {fd['dtE']:.2f}s")
        print(f"  State:    {fd['state']} ({fd['reason']})")
        print()
        # Patch P4: Interpretation
        dtheta = fd['delta_theta_deg_signed']
        if abs(dtheta) <= 45:
            interp = f"Micro-step ({'+' if dtheta > 0 else '-'}{abs(dtheta):.0f}° ≈ {abs(dtheta)/30:.1f} cycles)"
        else:
            interp = f"Larger movement ({dtheta:+.0f}°)"
        print(f"  → Interpretatie: {interp}")
        print(f"  → Signed Δθ voorkomt wrap artifacts (+345° → -15°)")
    else:
        print("  ⚠️  No displacement detected in entire session!")
        print("  → Mogelijk alleen scrape/touch activiteit")
    print()
    
    # State distribution
    print("─" * 70)
    print("  STATE DISTRIBUTION")
    print("─" * 70)
    for state, count in sorted(results["state_counts"].items(), key=lambda x: -x[1]):
        pct = count / results["total_records"] * 100
        bar = "█" * int(pct / 5)
        print(f"  {state:12} {count:6} ({pct:5.1f}%) {bar}")
    print()
    
    # Reason distribution
    print("─" * 70)
    print("  REASON DISTRIBUTION (top 8)")
    print("─" * 70)
    for reason, count in sorted(results["reason_counts"].items(), key=lambda x: -x[1])[:8]:
        pct = count / results["total_records"] * 100
        print(f"  {reason:30} {count:5} ({pct:4.1f}%)")
    print()
    
    # === DELTA THETA HISTOGRAM (Patch P1: signed) ===
    print("─" * 70)
    print("  Δθ HISTOGRAM (SIGNED, 15° buckets)")
    print("─" * 70)
    hist = results["delta_theta_histogram_signed"]
    if hist:
        max_count = max(hist.values()) if hist else 1
        for bucket in sorted(hist.keys()):
            count = hist[bucket]
            bar_len = int(count / max_count * 30)
            bar = "█" * bar_len
            print(f"  {bucket:+4.0f}° : {count:4} {bar}")
        print()
        print(f"  → Verwacht: ±15°, ±30°, ±45°... bij 24-magneet rotor (12 cycles/rot)")
    else:
        print("  No displacement events")
    print()
    
    # === DELTA CYCLES HISTOGRAM (Patch P3) ===
    print("─" * 70)
    print("  Δcycles HISTOGRAM")
    print("─" * 70)
    hist_cy = results["delta_cycles_histogram"]
    if hist_cy:
        max_count = max(hist_cy.values()) if hist_cy else 1
        for bucket in sorted(hist_cy.keys()):
            count = hist_cy[bucket]
            bar_len = int(count / max_count * 25)
            bar = "█" * bar_len
            print(f"  {bucket:+5.1f} : {count:4} {bar}")
        print()
        print(f"  → Verwacht: 0.5 of 1.0 bij normale stappen")
    else:
        print("  No cycle changes")
    print()
    
    # Top 10 delta theta (Patch P1: signed)
    print("─" * 70)
    print("  TOP 10 LARGEST |Δθ| (SIGNED)")
    print("─" * 70)
    for i, item in enumerate(results["top_delta_theta"][:10], 1):
        print(f"  {i:2}. t={item['t_abs_s']:6.2f}s  Δθ={item['delta_theta_deg_signed']:+6.1f}°  "
              f"Δcy={item['delta_cycles']:+.0f}  {item['state']}")
    if not results["top_delta_theta"]:
        print("  None")
    print()
    
    # === SCRAPE WITHOUT DISPLACEMENT (Patch P3) ===
    print("─" * 70)
    print("  SCRAPE WITHOUT DISPLACEMENT")
    print("─" * 70)
    swd_frac = results["scrape_without_disp_fraction"]
    print(f"  Fraction: {swd_frac:.1%} of high-activity ticks had Δcycles=0")
    if swd_frac > 0.5:
        print("  → Veel edge-oscillatie of threshold-scrape gedetecteerd")
    elif swd_frac > 0.2:
        print("  → Enige scrape activiteit")
    else:
        print("  → Meestal echte beweging bij hoge activiteit")
    print()
    
    # Scrape segments (Patch P2: clamped dtC)
    print("─" * 70)
    print("  SCRAPE SEGMENTS (activity without displacement)")
    print("─" * 70)
    scrapes = results["scrape_segments"]
    if scrapes:
        for seg in scrapes[:5]:
            print(f"  t={seg['t_start']:.1f}s → {seg['t_end']:.1f}s  "
                  f"dur={seg['duration_s']:.2f}s  max_dtC={seg['max_dtC']:.2f}s")
        if len(scrapes) > 5:
            print(f"  ... and {len(scrapes) - 5} more")
    else:
        print("  No significant scrape segments")
    print()
    
    # Direction flips
    print("─" * 70)
    print("  DIRECTION FLIPS")
    print("─" * 70)
    flips = results["direction_flips"]
    if flips:
        for flip in flips[:10]:
            print(f"  t={flip['t_abs_s']:.2f}s  {flip['from']} → {flip['to']}")
        if len(flips) > 10:
            print(f"  ... and {len(flips) - 10} more")
    else:
        print("  No direction flips")
    print()
    
    print("=" * 70)
    print()


def main():
    parser = argparse.ArgumentParser(
        description='Analyze handencoder log files (v2 - signed angles)'
    )
    parser.add_argument('files', nargs='+', help='JSONL log files')
    parser.add_argument('--summary', action='store_true',
                       help='Only show summary per file')
    parser.add_argument('--json', action='store_true',
                       help='Output as JSON')
    
    args = parser.parse_args()
    
    for filepath in args.files:
        if not Path(filepath).exists():
            print(f"❌ File not found: {filepath}")
            continue
        
        records = load_log(filepath)
        results = analyze_log(records)
        
        if args.json:
            results["delta_theta_histogram_signed"] = dict(results["delta_theta_histogram_signed"])
            results["delta_cycles_histogram"] = dict(results["delta_cycles_histogram"])
            results["state_counts"] = dict(results["state_counts"])
            results["reason_counts"] = dict(results["reason_counts"])
            print(json.dumps(results, indent=2))
        elif args.summary:
            fd = results.get("first_displacement")
            fd_str = f"t={fd['t_abs_s']:.1f}s Δθ={fd['delta_theta_deg_signed']:+.0f}°" if fd else "none"
            swd = results["scrape_without_disp_fraction"]
            print(f"{Path(filepath).name}: {results['duration_s']:.1f}s, "
                  f"{results['total_cycles']:.0f}cy, "
                  f"first: {fd_str}, "
                  f"scrape_no_disp: {swd:.0%}, "
                  f"flips: {len(results['direction_flips'])}")
        else:
            print_analysis(filepath, results)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
