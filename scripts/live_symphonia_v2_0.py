#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_symphonia_v2_0.py — Live ESP32 met L1 Encoder-Aware + L2 RealtimePipeline

S02.HandEncoder-Observability v0.4 — Patch D1/D3 implementatie.

Changelog v2.0.4:
- PATCH D1: Hard truth fields (delta_cycles, theta_deg, dt_since_event/cycle)
- PATCH D1: Scoreboard one-liner per tick
- PATCH D3: L2 debug passthrough tap (l2_extra in log)
- Alle v0.3 patches behouden (A/B/C)

Observability:
- Elke log regel bevat: events, cycles, Δcycles, Δθ°, dtE, dtC, reason
- Live scoreboard toont precies waarom L1 in bepaalde state is
"""

import os
import sys
import json
import time
import argparse
import struct
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Dict, Any, Optional

# === Symlink proof =================================================

HERE = Path(__file__).resolve()
env_root = os.getenv("SYMPHONIA_ROOT")
PROJECT_ROOT = None

if env_root:
    PROJECT_ROOT = Path(env_root).expanduser().resolve()
else:
    for parent in [HERE.parent, *HERE.parents]:
        if (parent / "sym_cycles").exists():
            PROJECT_ROOT = parent
            break

if PROJECT_ROOT is None:
    raise RuntimeError(
        "Kon 'sym_cycles' niet vinden. "
        "Zet SYMPHONIA_ROOT of zorg dat er ergens boven deze file een 'sym_cycles/' map staat."
    )

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# === BINLINK (inline) ========================================================

SYNC = 0xA5
TYPE_EVENT24 = 0x1
TYPE_EVENT16 = 0x0


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for ch in data:
        crc ^= (ch << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else ((crc << 1) & 0xFFFF)
    return crc


class FrameStream:
    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()

    def read_frames(self):
        chunk = self.ser.read(256)
        if chunk:
            self.buf.extend(chunk)
        
        while True:
            idx = self.buf.find(bytes([SYNC]))
            if idx < 0:
                self.buf.clear()
                break
            if idx > 0:
                del self.buf[:idx]
            if len(self.buf) < 4:
                break
            typever = self.buf[1]
            plen = self.buf[2]
            need = 1 + 1 + 1 + plen + 2
            if len(self.buf) < need:
                break
            frame = bytes(self.buf[:need])
            del self.buf[:need]
            
            crc_rx = struct.unpack('<H', frame[-2:])[0]
            crc_tx = crc16_ccitt_false(frame[1:3+plen])
            if crc_rx != crc_tx:
                continue
                
            t = (typever >> 4) & 0x0F
            v = typever & 0x0F
            payload = frame[3:-2]
            yield (t, v, payload)


def parse_event24(p):
    dt_us, = struct.unpack_from('<H', p, 0)
    tabs, = struct.unpack_from('<I', p, 2)
    flags0 = p[6]
    flags1 = p[7]
    return {
        "kind": "event24",
        "dt_us": dt_us,
        "t_abs_us": tabs,
        "flags0": flags0,
        "flags1": flags1,
        "sensor": (flags0 >> 3) & 1,
        "to_pool": (flags1 >> 4) & 0x3,
        "from_pool": (flags1 >> 6) & 0x3,
    }


def parse_event16(p):
    dt_us, = struct.unpack_from('<H', p, 0)
    flags0 = p[2]
    flags1 = p[3]
    return {
        "kind": "event16",
        "dt_us": dt_us,
        "flags0": flags0,
        "flags1": flags1,
        "sensor": (flags0 >> 3) & 1,
        "to_pool": (flags1 >> 4) & 0x3,
        "from_pool": (flags1 >> 6) & 0x3,
    }


def decode_flags(flags0, flags1):
    return {
        "pair": (flags0 >> 7) & 1,
        "qlevel": (flags0 >> 5) & 0x3,
        "polarity": (flags0 >> 4) & 1,
        "sensor": (flags0 >> 3) & 1,
        "from_pool": (flags1 >> 6) & 0x3,
        "to_pool": (flags1 >> 4) & 0x3,
        "dir_hint": (flags1 >> 2) & 0x3,
        "edge_kind": (flags1 >> 0) & 0x3,
    }


# === L1 PHYSICAL ACTIVITY (canonical import) =================================

try:
    from sym_cycles.l1_physical_activity import (
        L1PhysicalActivity,
        L1Config,
        L1State,
        L1Snapshot,
        L1Reason,
    )
except ImportError:
    try:
        from l1_physical_activity import (
            L1PhysicalActivity,
            L1Config,
            L1State,
            L1Snapshot,
            L1Reason,
        )
    except ImportError:
        raise ImportError(
            "❌ l1_physical_activity.py niet gevonden!\n"
            "   Zorg dat dit bestand in sym_cycles/ of dezelfde directory staat."
        )


# === TERMINAL UI =============================================================

class TerminalUI:
    CLEAR_LINE = "\033[K"
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    DIM = "\033[2m"
    BLUE = "\033[94m"
    
    def __init__(self, num_lines=20):
        self.num_lines = num_lines
        self.initialized = False
        
    def init(self):
        print(self.HIDE_CURSOR, end='')
        for _ in range(self.num_lines):
            print()
        self.initialized = True
        
    def update(self, lines: list):
        if not self.initialized:
            self.init()
        print(f"\033[{self.num_lines}A", end='')
        for line in lines[:self.num_lines]:
            print(f"{self.CLEAR_LINE}{line}")
        for _ in range(self.num_lines - len(lines)):
            print(self.CLEAR_LINE)
            
    def cleanup(self):
        print(self.SHOW_CURSOR, end='')


def format_display(
    l1_snap: L1Snapshot,
    l2_snap: dict,
    dt_since_event_s: float,
    dt_since_cycle_s: float,
    events_per_sec: float,
    elapsed: float,
) -> list:
    """Format display met hard truth fields (Patch D1)."""
    ui = TerminalUI
    lines = []
    
    # Header
    lines.append(f"{ui.BOLD}═══════════════════════════════════════════════════════════════{ui.RESET}")
    lines.append(f"{ui.BOLD}  SYMPHONIA v2.0.4 — Observability{ui.RESET}")
    lines.append(f"═══════════════════════════════════════════════════════════════")
    
    # L1 State + Reason (Patch D2)
    l1_state_str = l1_snap.state.value
    reason_str = l1_snap.reason.value if hasattr(l1_snap.reason, 'value') else str(l1_snap.reason)
    
    state_colors = {
        "STILL": ui.DIM, "FEELING": ui.BLUE, "SCRAPE": ui.YELLOW,
        "DISPLACEMENT": ui.MAGENTA, "MOVING": ui.GREEN,
    }
    state_icons = {
        "STILL": "○", "FEELING": "◐", "SCRAPE": "◎",
        "DISPLACEMENT": "◑", "MOVING": "◉",
    }
    
    l1_color = state_colors.get(l1_state_str, ui.RESET)
    l1_icon = state_icons.get(l1_state_str, "?")
    
    lines.append(f"  {ui.BOLD}L1:{ui.RESET} {l1_color}{l1_icon} {l1_state_str:<12}{ui.RESET} reason={ui.DIM}{reason_str}{ui.RESET}")
    
    # θ̂ in degrees (Patch D1)
    lines.append(f"  θ̂:  {l1_snap.theta_hat_deg:7.1f}°    Δθ̂: {l1_snap.delta_theta_deg:+6.1f}°")
    lines.append(f"  act: {l1_snap.activity_score:7.2f}     disp: {l1_snap.disp_score:.5f}")
    
    # Encoder conf bar
    conf_bar_len = 20
    conf_filled = int(l1_snap.encoder_conf * conf_bar_len)
    conf_bar = "█" * conf_filled + "░" * (conf_bar_len - conf_filled)
    lines.append(f"  conf:[{conf_bar}] {l1_snap.encoder_conf:.2f}")
    
    lines.append(f"───────────────────────────────────────────────────────────────")
    
    # Time gaps (Patch D1: hard truth)
    lines.append(f"  {ui.BOLD}Gaps:{ui.RESET} dtE={dt_since_event_s:.2f}s  dtC={dt_since_cycle_s:.2f}s  Δcy={l1_snap.delta_cycles:+.0f}")
    
    lines.append(f"───────────────────────────────────────────────────────────────")
    
    # L2 Awareness
    rotor = l2_snap.get("rotor_state", "STILL")
    lock = l2_snap.get("direction_lock_state", "UNLOCKED")
    direction = l2_snap.get("direction_global_effective", "UNDECIDED")
    rpm = l2_snap.get("rpm_est", 0)
    
    rotor_color = ui.GREEN if rotor == "MOVEMENT" else ui.DIM
    lock_color = ui.GREEN if lock == "LOCKED" else (ui.YELLOW if lock == "SOFT_LOCK" else ui.DIM)
    dir_color = ui.CYAN if direction in ("CW", "CCW") else ui.DIM
    
    lines.append(f"  {ui.BOLD}L2:{ui.RESET} {rotor_color}{rotor:<10}{ui.RESET} {lock_color}{lock:<10}{ui.RESET} {dir_color}{direction:<8}{ui.RESET}")
    
    # L2 metrics
    compass = l2_snap.get("compass_snapshot")
    score = compass.global_score if compass else 0
    cycles_total = l2_snap.get("total_cycles_physical", 0)
    
    lines.append(f"  rpm: {rpm:6.1f}  score: {score:+.3f}  cycles: {cycles_total:.0f}")
    
    lines.append(f"───────────────────────────────────────────────────────────────")
    
    # Stats
    lines.append(f"  Events/s: {events_per_sec:6.1f}    Tijd: {elapsed:.1f}s")
    
    lines.append(f"═══════════════════════════════════════════════════════════════")
    
    # Scoreboard one-liner (Patch D1.2)
    scoreboard = (
        f"L1 {l1_state_str:12} act={l1_snap.activity_score:5.1f} "
        f"dθ={l1_snap.delta_theta_deg:+5.1f}° conf={l1_snap.encoder_conf:.2f} | "
        f"L2 {rotor}/{lock} Δcy={l1_snap.delta_cycles:+.0f} "
        f"dtE={dt_since_event_s:.2f}s dtC={dt_since_cycle_s:.2f}s"
    )
    lines.append(f"{ui.DIM}{scoreboard}{ui.RESET}")
    
    return lines


# === MAIN ====================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Live ESP32 met L1 Encoder-Aware (Observability v0.4)'
    )
    parser.add_argument('--port', '-p', default='/dev/ttyUSB0')
    parser.add_argument('--baud', '-b', type=int, default=115200)
    parser.add_argument('--profile', choices=['production', 'bench', 'bench_tolerant'],
                       default='bench_tolerant')
    parser.add_argument('--gap-ms', type=float, default=500.0)
    parser.add_argument('--disp-threshold', type=float, default=0.005)
    parser.add_argument('--encoder-tau', type=float, default=0.6)
    parser.add_argument('--hard-reset', type=float, default=1.5)
    parser.add_argument('--tick-ms', type=float, default=50.0)
    parser.add_argument('--min-normal-tile', type=int, default=2)
    parser.add_argument('--log', '-l', action='store_true')
    parser.add_argument('--simple', '-s', action='store_true')
    parser.add_argument('--scoreboard', action='store_true',
                       help='Print scoreboard line per tick')
    
    args = parser.parse_args()
    
    # Import serial
    try:
        import serial
    except ImportError:
        print("❌ pyserial niet geïnstalleerd!")
        return 1
    
    # Import L2 pipeline
    try:
        from sym_cycles.realtime_states_v1_9_canonical import (
            RealtimePipeline, PipelineProfile,
            PROFILE_PRODUCTION, PROFILE_BENCH, PROFILE_BENCH_TOLERANT,
        )
    except ImportError:
        import importlib.util
        base_dir = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            "realtime_states",
            base_dir / "realtime_states_v1_9_canonical.py"
        )
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            RealtimePipeline = module.RealtimePipeline
            PipelineProfile = module.PipelineProfile
            PROFILE_PRODUCTION = module.PROFILE_PRODUCTION
            PROFILE_BENCH = module.PROFILE_BENCH
            PROFILE_BENCH_TOLERANT = module.PROFILE_BENCH_TOLERANT
        else:
            print("❌ realtime_states_v1_9_canonical.py niet gevonden!")
            return 1
    
    # Select L2 profile
    if args.profile == 'bench_tolerant':
        l2_profile = PROFILE_BENCH_TOLERANT
    elif args.profile == 'bench':
        l2_profile = PROFILE_BENCH
    else:
        l2_profile = PROFILE_PRODUCTION
    
    # Create L1 config
    l1_config = L1Config(
        gap_ms=args.gap_ms,
        displacement_threshold=args.disp_threshold,
        activity_threshold_low=1.0,
        activity_threshold_high=5.0,
        direction_conf_threshold=0.5,
        cycles_per_rot=l2_profile.cycles_per_rot,
        encoder_tau_s=args.encoder_tau,
        hard_reset_s=args.hard_reset,
        activity_decay_rate=5.0,
    )
    
    # Open serial
    print(f"[i] Opening {args.port} @ {args.baud}...")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.01)
    except Exception as e:
        print(f"❌ {e}")
        return 1
    
    # Create components
    fs = FrameStream(ser)
    l2_pipeline = RealtimePipeline(profile=l2_profile)
    l1_activity = L1PhysicalActivity(config=l1_config)
    
    # Setup logging
    log_file = None
    if args.log:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = f"live_encoder_{timestamp}.jsonl"
        log_file = open(log_path, 'w')
        print(f"[i] Logging to: {log_path}")
    
    # Setup UI
    ui = None if args.simple or args.scoreboard else TerminalUI(num_lines=20)
    
    print(f"[i] L2 Profile: {l2_profile.name}")
    print(f"[i] L1: gap={args.gap_ms}ms, D0={args.disp_threshold}, tau={args.encoder_tau}s")
    print(f"[i] Listening... (Ctrl+C to stop)")
    print()
    
    if ui:
        ui.init()
    
    # State
    t0 = time.time()
    last_display = time.time()
    last_tick = time.time()
    tick_interval_s = args.tick_ms / 1000.0
    
    events_window = deque(maxlen=100)
    total_events = 0
    
    # Hard truth trackers (Patch D1)
    dt_since_last_event_s = 0.0
    dt_since_last_cycle_s = 0.0
    prev_cycles_physical_total = 0.0
    
    l1_snap = L1Snapshot(state=L1State.STILL, reason=L1Reason.INIT)
    l2_snap = {}
    cycles_physical_total = 0.0
    
    try:
        while True:
            now = time.time()
            events_this_batch = 0
            
            # Process frames
            for frame_type, ver, payload in fs.read_frames():
                if frame_type == TYPE_EVENT24:
                    ev = parse_event24(payload)
                    ev.update(decode_flags(ev["flags0"], ev["flags1"]))
                elif frame_type == TYPE_EVENT16:
                    ev = parse_event16(payload)
                    ev.update(decode_flags(ev["flags0"], ev["flags1"]))
                    ev["t_abs_us"] = int((now - t0) * 1e6)
                else:
                    continue
                
                events_window.append(now)
                total_events += 1
                events_this_batch += 1
                
                # Feed to L2
                l2_result = l2_pipeline.feed_event(ev)
                l2_snap = l2_result.movement_state
                l2_snap["compass_snapshot"] = l2_result.compass_snapshot
                cycles_physical_total = l2_snap.get("total_cycles_physical", 0)
            
            # === PATCH D1: Hard truth time gaps ===
            delta_cycles_physical = cycles_physical_total - prev_cycles_physical_total
            
            if events_this_batch > 0:
                dt_since_last_event_s = 0.0
            else:
                dt_since_last_event_s += tick_interval_s
            
            if delta_cycles_physical > 0:
                dt_since_last_cycle_s = 0.0
                prev_cycles_physical_total = cycles_physical_total
            else:
                dt_since_last_cycle_s += tick_interval_s
            
            # === Update L1 (Patch C: idle tick) ===
            if now - last_tick >= tick_interval_s or events_this_batch > 0:
                last_tick = now
                
                compass_snap = l2_snap.get("compass_snapshot")
                direction_conf = abs(getattr(compass_snap, 'global_score', 0.0)) if compass_snap else 0.0
                lock_state = l2_snap.get("direction_lock_state", "UNLOCKED")
                direction_effective = l2_snap.get("direction_global_effective", "UNDECIDED")
                
                l1_snap = l1_activity.update(
                    wall_time=now,
                    cycles_physical_total=cycles_physical_total,
                    events_this_batch=events_this_batch,
                    direction_conf=direction_conf,
                    lock_state=lock_state,
                    direction_effective=direction_effective,
                )
                
                # === PATCH D3: L2 debug passthrough tap ===
                l2_extra_keys = [
                    "rpm_est", "rotations", "direction_conf",
                    "compass_score", "cadence_ok", "cycle_index",
                    "tiles_emitted_n", "cycles_emitted_n",
                    "total_cycles_physical", "cycles_claimed_at_lock",
                ]
                l2_extra = {k: l2_snap[k] for k in l2_extra_keys if k in l2_snap}
                
                # Also extract compass fields if available
                if compass_snap:
                    l2_extra["compass_global_score"] = getattr(compass_snap, 'global_score', None)
                    l2_extra["compass_confidence"] = getattr(compass_snap, 'confidence', None)
                
                # === Log (Patch D1 + D3) ===
                if log_file:
                    log_entry = {
                        "t_abs_s": round(now - t0, 4),
                        "dt_s": round(l1_snap.dt_s, 4),
                        "events_this_batch": events_this_batch,
                        "cycles_physical_total": cycles_physical_total,
                        "delta_cycles_physical": delta_cycles_physical,
                        "dt_since_last_event_s": round(dt_since_last_event_s, 3),
                        "dt_since_last_cycle_s": round(dt_since_last_cycle_s, 3),
                        "l1": {
                            "state": l1_snap.state.value,
                            "reason": l1_snap.reason.value,
                            "theta_hat_deg": round(l1_snap.theta_hat_deg, 2),
                            "delta_theta_deg": round(l1_snap.delta_theta_deg, 2),
                            "activity_score": round(l1_snap.activity_score, 2),
                            "disp_score": round(l1_snap.disp_score, 6),
                            "encoder_conf": round(l1_snap.encoder_conf, 3),
                        },
                        "l2": {
                            "rotor_state": l2_snap.get("rotor_state", "STILL"),
                            "lock_state": lock_state,
                            "direction": direction_effective,
                        },
                        "l2_extra": l2_extra,
                    }
                    log_file.write(json.dumps(log_entry) + "\n")
                
                # === Scoreboard mode (Patch D1.2) ===
                if args.scoreboard:
                    rpm = l2_snap.get("rpm_est", 0)
                    rotor = l2_snap.get("rotor_state", "STILL")
                    rpm_str = f"rpm={rpm:.0f}" if rpm else ""
                    
                    line = (
                        f"L1 {l1_snap.state.value:12} act={l1_snap.activity_score:5.1f} "
                        f"dθ={l1_snap.delta_theta_deg:+6.1f}° conf={l1_snap.encoder_conf:.2f} | "
                        f"L2 {rotor}/{lock_state} dir={direction_effective} {rpm_str} "
                        f"Δcy={delta_cycles_physical:+.0f} "
                        f"dtE={dt_since_last_event_s:.2f}s dtC={dt_since_last_cycle_s:.2f}s"
                    )
                    print(line)
            
            # Update display
            if ui and now - last_display > 0.1:
                elapsed = now - t0
                recent = [t for t in events_window if now - t < 1.0]
                events_per_sec = len(recent)
                
                lines = format_display(
                    l1_snap, l2_snap,
                    dt_since_last_event_s, dt_since_last_cycle_s,
                    events_per_sec, elapsed
                )
                ui.update(lines)
                last_display = now
            
            elif args.simple and now - last_display > 0.1:
                elapsed = now - t0
                recent = [t for t in events_window if now - t < 1.0]
                events_per_sec = len(recent)
                
                print(f"\r[{elapsed:6.1f}s] L1:{l1_snap.state.value:12} "
                      f"θ̂={l1_snap.theta_hat_deg:5.1f}° Δθ={l1_snap.delta_theta_deg:+5.1f}° "
                      f"conf={l1_snap.encoder_conf:.2f} "
                      f"Δcy={delta_cycles_physical:+.0f} dtC={dt_since_last_cycle_s:.1f}s | "
                      f"ev/s={events_per_sec:3.0f}   ",
                      end='', flush=True)
                last_display = now
    
    except KeyboardInterrupt:
        print("\n\n[i] Stopped")
    
    finally:
        if ui:
            ui.cleanup()
        ser.close()
        if log_file:
            log_file.close()
        
        # Summary
        elapsed = time.time() - t0
        print()
        print("=" * 65)
        print("SESSION SUMMARY")
        print("=" * 65)
        print(f"  Duration:        {elapsed:.1f}s")
        print(f"  Total events:    {total_events}")
        print(f"  Total cycles:    {cycles_physical_total:.0f}")
        print()
        print(f"  L1 Final:        {l1_snap.state.value} ({l1_snap.reason.value})")
        print(f"  L1 θ̂:            {l1_snap.theta_hat_deg:.1f}°")
        print(f"  L1 Encoder Conf: {l1_snap.encoder_conf:.3f}")
        print()
        final_l2 = l2_pipeline.snapshot().movement_state
        print(f"  L2 Final:        {final_l2.get('rotor_state', 'STILL')} / "
              f"{final_l2.get('direction_lock_state', 'UNLOCKED')}")
        print("=" * 65)
        
        if args.log:
            print(f"\n[i] Log: live_encoder_*.jsonl")
            print(f"[i] Analyze: python3 analyze_handencoder_log.py <logfile>")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
