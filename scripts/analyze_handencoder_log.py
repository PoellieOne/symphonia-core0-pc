#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_handencoder_log.py — Offline analyzer voor handencoder logs

S02.HandEncoder-Observability v0.4 — Patch D4

Analyseert live_encoder_*.jsonl en toont:
1. First displacement (eerste Δθ≠0)
2. Histogram van delta_theta_deg
3. Top 10 grootste |Δθ|
4. Scrape segments (activity hoog, dtC stijgt)
5. Direction flips (CW↔CCW)

Gebruik:
    python3 analyze_handencoder_log.py live_encoder_20251212_120306.jsonl
    python3 analyze_handencoder_log.py *.jsonl --summary
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from typing import List, Dict, Any, Optional


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
    """Analyze log records and return summary."""
    if not records:
        return {"error": "No records"}
    
    results = {
        "total_records": len(records),
        "duration_s": 0,
        "first_displacement": None,
        "total_cycles": 0,
        "delta_theta_histogram": defaultdict(int),
        "top_delta_theta": [],
        "scrape_segments": [],
        "direction_flips": [],
        "state_counts": Counter(),
        "reason_counts": Counter(),
    }
    
    # Duration
    if records:
        t_start = records[0].get("t_abs_s", 0)
        t_end = records[-1].get("t_abs_s", 0)
        results["duration_s"] = t_end - t_start
    
    # Track state
    prev_direction = None
    scrape_start = None
    scrape_max_dtC = 0
    
    for i, rec in enumerate(records):
        t = rec.get("t_abs_s", 0)
        l1 = rec.get("l1", {})
        l2 = rec.get("l2", {})
        
        state = l1.get("state", "STILL")
        reason = l1.get("reason", "")
        delta_theta_deg = l1.get("delta_theta_deg", 0)
        delta_cycles = rec.get("delta_cycles_physical", 0)
        dtC = rec.get("dt_since_last_cycle_s", 0)
        direction = l2.get("direction", "UNDECIDED")
        
        # State counts
        results["state_counts"][state] += 1
        results["reason_counts"][reason] += 1
        
        # First displacement
        if results["first_displacement"] is None and abs(delta_theta_deg) > 0.1:
            results["first_displacement"] = {
                "t_abs_s": t,
                "record_index": i,
                "delta_theta_deg": delta_theta_deg,
                "state": state,
            }
        
        # Total cycles
        results["total_cycles"] = rec.get("cycles_physical_total", 0)
        
        # Delta theta histogram (bucket by 5°)
        if abs(delta_theta_deg) > 0.1:
            bucket = round(delta_theta_deg / 5) * 5
            results["delta_theta_histogram"][bucket] += 1
            
            # Track for top 10
            results["top_delta_theta"].append({
                "t_abs_s": t,
                "delta_theta_deg": delta_theta_deg,
                "state": state,
            })
        
        # Scrape detection (activity but dtC rising)
        if state == "SCRAPE":
            if scrape_start is None:
                scrape_start = t
                scrape_max_dtC = dtC
            else:
                scrape_max_dtC = max(scrape_max_dtC, dtC)
        else:
            if scrape_start is not None and scrape_max_dtC > 0.5:
                results["scrape_segments"].append({
                    "t_start": scrape_start,
                    "t_end": t,
                    "duration_s": t - scrape_start,
                    "max_dtC": scrape_max_dtC,
                })
            scrape_start = None
            scrape_max_dtC = 0
        
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
        key=lambda x: abs(x["delta_theta_deg"]),
        reverse=True
    )[:10]
    
    return results


def print_analysis(filepath: str, results: Dict[str, Any]):
    """Print analysis results in human-readable format."""
    print()
    print("=" * 70)
    print(f"  HANDENCODER LOG ANALYSIS: {Path(filepath).name}")
    print("=" * 70)
    print()
    
    # Summary
    print(f"  Duration:      {results['duration_s']:.1f}s")
    print(f"  Records:       {results['total_records']}")
    print(f"  Total cycles:  {results['total_cycles']:.0f}")
    print()
    
    # First displacement
    print("─" * 70)
    print("  FIRST DISPLACEMENT")
    print("─" * 70)
    fd = results.get("first_displacement")
    if fd:
        print(f"  t={fd['t_abs_s']:.2f}s  Δθ={fd['delta_theta_deg']:+.1f}°  state={fd['state']}")
    else:
        print("  ⚠️  No displacement detected!")
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
    print("  REASON DISTRIBUTION")
    print("─" * 70)
    for reason, count in sorted(results["reason_counts"].items(), key=lambda x: -x[1])[:8]:
        pct = count / results["total_records"] * 100
        print(f"  {reason:30} {count:5} ({pct:4.1f}%)")
    print()
    
    # Delta theta histogram
    print("─" * 70)
    print("  Δθ HISTOGRAM (degrees)")
    print("─" * 70)
    hist = results["delta_theta_histogram"]
    if hist:
        max_count = max(hist.values()) if hist else 1
        for bucket in sorted(hist.keys()):
            count = hist[bucket]
            bar_len = int(count / max_count * 30)
            bar = "█" * bar_len
            print(f"  {bucket:+4.0f}° : {count:4} {bar}")
    else:
        print("  No displacement events")
    print()
    
    # Top 10 delta theta
    print("─" * 70)
    print("  TOP 10 LARGEST |Δθ|")
    print("─" * 70)
    for i, item in enumerate(results["top_delta_theta"][:10], 1):
        print(f"  {i:2}. t={item['t_abs_s']:6.2f}s  Δθ={item['delta_theta_deg']:+6.1f}°  {item['state']}")
    if not results["top_delta_theta"]:
        print("  None")
    print()
    
    # Scrape segments
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
        description='Analyze handencoder log files'
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
            # Convert defaultdict and Counter for JSON
            results["delta_theta_histogram"] = dict(results["delta_theta_histogram"])
            results["state_counts"] = dict(results["state_counts"])
            results["reason_counts"] = dict(results["reason_counts"])
            print(json.dumps(results, indent=2))
        elif args.summary:
            fd = results.get("first_displacement")
            fd_str = f"t={fd['t_abs_s']:.1f}s Δθ={fd['delta_theta_deg']:+.0f}°" if fd else "none"
            print(f"{Path(filepath).name}: {results['duration_s']:.1f}s, "
                  f"{results['total_cycles']:.0f} cycles, "
                  f"first_disp: {fd_str}, "
                  f"scrapes: {len(results['scrape_segments'])}, "
                  f"flips: {len(results['direction_flips'])}")
        else:
            print_analysis(filepath, results)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
