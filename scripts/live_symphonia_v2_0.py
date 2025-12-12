#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_symphonia_v2_0.py — Live ESP32 met L1 PhysicalActivity + L2 RealtimePipeline

S02.HandEncoder-Awareness v0.1 implementatie.

Changelog v2.0.2:
- L1 v0.2: 5-state model (STILL/FEELING/SCRAPE/DISPLACEMENT/MOVING)
- L1 gevoed met raw EVENT24 frame counts (events_this_batch)
- L1 ontvangt L2 doorlus: rotations, direction_conf, lock_state, direction_effective
- Display toont encoder-achtige observables: θ̂, Δθ̂, activity_score, encoder_conf

Integreert:
- L1: PhysicalActivity v0.2 (encoder-aware) via canonical l1_physical_activity.py
- L2: RealtimePipeline v1.9 (Cycles → Tiles → Compass → MovementBody)

Gebruik:
    python3 live_symphonia_v2_0.py [--port /dev/ttyUSB0] [--profile bench_tolerant]
    python3 live_symphonia_v2_0.py --gap-ms 500 --log
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
from dataclasses import dataclass
from typing import Dict, Any, Optional, List

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
    )
except ImportError:
    try:
        from l1_physical_activity import (
            L1PhysicalActivity,
            L1Config,
            L1State,
            L1Snapshot,
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
    
    def __init__(self, num_lines=18):
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
    events_per_sec: float,
    elapsed: float,
) -> list:
    """Format L1 + L2 state voor terminal display (encoder-aware)."""
    ui = TerminalUI
    lines = []
    
    # Header
    lines.append(f"{ui.BOLD}═══════════════════════════════════════════════════════════════{ui.RESET}")
    lines.append(f"{ui.BOLD}  SYMPHONIA v2.0 — L1 Encoder-Aware + L2 Awareness{ui.RESET}")
    lines.append(f"═══════════════════════════════════════════════════════════════")
    
    # L1 State (5-state model)
    l1_state = l1_snap.state
    l1_state_str = l1_state.value if hasattr(l1_state, 'value') else str(l1_state)
    
    state_colors = {
        "STILL": ui.DIM,
        "FEELING": ui.BLUE,
        "SCRAPE": ui.YELLOW,
        "DISPLACEMENT": ui.MAGENTA,
        "MOVING": ui.GREEN,
    }
    state_icons = {
        "STILL": "○",
        "FEELING": "◐",
        "SCRAPE": "◎",
        "DISPLACEMENT": "◑",
        "MOVING": "◉",
    }
    
    l1_color = state_colors.get(l1_state_str, ui.RESET)
    l1_icon = state_icons.get(l1_state_str, "?")
    
    lines.append(f"  {ui.BOLD}L1 State:{ui.RESET}     {l1_color}{l1_icon} {l1_state_str:<12}{ui.RESET}")
    
    # L1 Encoder metrics
    theta_hat = l1_snap.theta_hat_rot
    delta_theta = l1_snap.delta_theta_rot
    activity = l1_snap.activity_score
    disp = l1_snap.disp_score
    enc_conf = l1_snap.encoder_conf
    
    lines.append(f"  θ̂:           {theta_hat:+8.3f} rot   Δθ̂: {delta_theta:+.4f}")
    lines.append(f"  Activity:    {activity:8.1f} ev/s  Disp: {disp:.4f}")
    lines.append(f"  Encoder Conf: {enc_conf:7.2f}")
    
    lines.append(f"───────────────────────────────────────────────────────────────")
    
    # L2 Awareness
    rotor = l2_snap.get("rotor_state", "STILL")
    lock = l2_snap.get("direction_lock_state", "UNLOCKED")
    direction = l2_snap.get("direction_global_effective", "UNDECIDED")
    
    rotor_color = ui.GREEN if rotor == "MOVEMENT" else ui.DIM
    
    if lock == "LOCKED":
        lock_color = ui.GREEN
    elif lock == "SOFT_LOCK":
        lock_color = ui.YELLOW
    else:
        lock_color = ui.DIM
    
    dir_color = ui.CYAN if direction in ("CW", "CCW") else ui.DIM
    
    lines.append(f"  {ui.BOLD}L2 Awareness:{ui.RESET} {rotor_color}{rotor:<12}{ui.RESET}")
    lines.append(f"  └─ Lock:       {lock_color}{lock:<12}{ui.RESET}")
    lines.append(f"     └─ Dir:     {dir_color}{direction:<12}{ui.RESET}")
    
    lines.append(f"───────────────────────────────────────────────────────────────")
    
    # Metrics
    rotations = l2_snap.get("rotations", 0)
    cycles_total = l2_snap.get("total_cycles_physical", 0)
    rpm = l2_snap.get("rpm_est", 0)
    
    compass = l2_snap.get("compass_snapshot")
    score = compass.global_score if compass else 0
    
    lines.append(f"  Rotaties:   {ui.BOLD}{rotations:+8.2f}{ui.RESET}     Cycles: {cycles_total:.0f}")
    lines.append(f"  RPM:        {rpm:8.1f}       Score: {score:+.3f}")
    lines.append(f"  Events/s:   {events_per_sec:8.1f}       Tijd: {elapsed:.1f}s")
    
    lines.append(f"═══════════════════════════════════════════════════════════════")
    
    return lines


# === MAIN ====================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Live ESP32 met L1 Encoder-Aware + L2 RealtimePipeline'
    )
    parser.add_argument('--port', '-p', default='/dev/ttyUSB0')
    parser.add_argument('--baud', '-b', type=int, default=115200)
    parser.add_argument('--profile', choices=['production', 'bench', 'bench_tolerant'],
                       default='bench_tolerant')
    parser.add_argument('--gap-ms', type=float, default=500.0,
                       help='L1 gap threshold (default: 500)')
    parser.add_argument('--disp-threshold', type=float, default=0.01,
                       help='L1 displacement threshold (default: 0.01 rot)')
    parser.add_argument('--min-normal-tile', type=int, default=2)
    parser.add_argument('--log', '-l', action='store_true',
                       help='Log to JSONL file')
    parser.add_argument('--simple', '-s', action='store_true')
    
    args = parser.parse_args()
    
    # Import serial
    try:
        import serial
    except ImportError:
        print("❌ pyserial niet geïnstalleerd!")
        print("   pip install pyserial")
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
    
    # Apply min_normal_tile override
    if args.min_normal_tile != l2_profile.min_normal_tile:
        l2_profile = PipelineProfile(
            name=f"{l2_profile.name}-custom",
            compass_alpha=l2_profile.compass_alpha,
            compass_threshold_high=l2_profile.compass_threshold_high,
            compass_threshold_low=l2_profile.compass_threshold_low,
            compass_window_tiles=l2_profile.compass_window_tiles,
            compass_deadzone_us=l2_profile.compass_deadzone_us,
            compass_min_tiles=l2_profile.compass_min_tiles,
            compass_max_abs_dt_us=l2_profile.compass_max_abs_dt_us,
            lock_confidence_threshold=l2_profile.lock_confidence_threshold,
            lock_soft_threshold=l2_profile.lock_soft_threshold,
            unlock_tiles_threshold=l2_profile.unlock_tiles_threshold,
            rpm_alpha=l2_profile.rpm_alpha,
            jitter_max_rel=l2_profile.jitter_max_rel,
            jitter_window_size=l2_profile.jitter_window_size,
            rpm_move_thresh=l2_profile.rpm_move_thresh,
            rpm_slow_thresh=l2_profile.rpm_slow_thresh,
            rpm_still_thresh=l2_profile.rpm_still_thresh,
            tile_span_cycles=l2_profile.tile_span_cycles,
            min_normal_tile=args.min_normal_tile,
            stereo_fusion=l2_profile.stereo_fusion,
            cycles_per_rot=l2_profile.cycles_per_rot,
        )
    
    # Create L1 config (encoder-aware v0.2)
    l1_config = L1Config(
        gap_ms=args.gap_ms,
        displacement_threshold=args.disp_threshold,
        activity_threshold_low=1.0,
        activity_threshold_high=5.0,
        direction_conf_threshold=0.5,
        cycles_per_rot=l2_profile.cycles_per_rot,
    )
    
    # Open serial
    print(f"[i] Opening {args.port} @ {args.baud}...")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.05)
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
    ui = None if args.simple else TerminalUI(num_lines=18)
    
    print(f"[i] L2 Profile: {l2_profile.name}")
    print(f"[i] L1 Config: gap={args.gap_ms}ms, disp_thresh={args.disp_threshold}")
    print(f"[i] L1 Source: canonical l1_physical_activity.py v0.2 (encoder-aware)")
    print(f"[i] Listening... (Ctrl+C to stop)")
    print()
    print(f"[i] L1 States: ○STILL → ◐FEELING → ◎SCRAPE → ◑DISPLACEMENT → ◉MOVING")
    print()
    
    if ui:
        ui.init()
    
    # Statistics
    t0 = time.time()
    last_display = time.time()
    events_window = deque(maxlen=100)
    total_events = 0
    
    l1_snap = L1Snapshot(state=L1State.STILL)
    l2_snap = {}
    
    try:
        while True:
            now = time.time()
            events_this_batch = 0  # Raw EVENT24 frame count!
            
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
            
            # === Update L1 met alle benodigde data ===
            cycles_total = l2_snap.get("total_cycles_physical", 0)
            rotations = l2_snap.get("rotations", 0)
            
            # Direction confidence van compass
            compass_snap = l2_snap.get("compass_snapshot")
            direction_conf = 0.0
            if compass_snap:
                direction_conf = getattr(compass_snap, 'confidence', 0.0)
                if direction_conf == 0.0:
                    # Fallback: gebruik global_score magnitude als proxy
                    direction_conf = abs(getattr(compass_snap, 'global_score', 0.0))
            
            lock_state = l2_snap.get("direction_lock_state", "UNLOCKED")
            direction_effective = l2_snap.get("direction_global_effective", "UNDECIDED")
            
            l1_snap = l1_activity.update(
                wall_time=now,
                cycles_physical_total=cycles_total,
                events_this_batch=events_this_batch,  # Raw EVENT24 frames!
                rotations=rotations,
                direction_conf=direction_conf,
                lock_state=lock_state,
                direction_effective=direction_effective,
            )
            
            # Log
            if log_file and events_this_batch > 0:
                log_entry = {
                    "t": now - t0,
                    "events_batch": events_this_batch,
                    "l1": {
                        "state": l1_snap.state.value,
                        "theta_hat_rot": l1_snap.theta_hat_rot,
                        "delta_theta_rot": l1_snap.delta_theta_rot,
                        "activity_score": l1_snap.activity_score,
                        "disp_score": l1_snap.disp_score,
                        "encoder_conf": l1_snap.encoder_conf,
                    },
                    "l2": {
                        "rotor_state": l2_snap.get("rotor_state"),
                        "lock_state": lock_state,
                        "direction": direction_effective,
                        "rotations": rotations,
                        "rpm_est": l2_snap.get("rpm_est", 0),
                    }
                }
                log_file.write(json.dumps(log_entry) + "\n")
            
            # Update display
            if now - last_display > 0.1:
                elapsed = now - t0
                recent = [t for t in events_window if now - t < 1.0]
                events_per_sec = len(recent)
                
                if ui:
                    lines = format_display(l1_snap, l2_snap, events_per_sec, elapsed)
                    ui.update(lines)
                elif args.simple:
                    l1_state_str = l1_snap.state.value
                    rotor = l2_snap.get("rotor_state", "STILL")
                    lock = l2_snap.get("direction_lock_state", "UNLOCKED")
                    theta = l1_snap.theta_hat_rot
                    
                    print(f"\r[{elapsed:6.1f}s] L1:{l1_state_str:12} θ̂={theta:+.2f} | "
                          f"L2:{rotor:9}/{lock:10} | ev/s={events_per_sec:3.0f}   ",
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
        
        # Final summary
        elapsed = time.time() - t0
        final_l2 = l2_pipeline.snapshot().movement_state
        
        print()
        print("=" * 65)
        print("SESSION SUMMARY")
        print("=" * 65)
        print(f"  Duration:        {elapsed:.1f}s")
        print(f"  Total events:    {total_events}")
        print(f"  Total cycles:    {final_l2.get('total_cycles_physical', 0):.0f}")
        print()
        print(f"  L1 Final State:  {l1_snap.state.value}")
        print(f"  L1 θ̂ (rot):      {l1_snap.theta_hat_rot:.3f}")
        print(f"  L1 Encoder Conf: {l1_snap.encoder_conf:.2f}")
        print()
        print(f"  L2 Final:        {final_l2.get('rotor_state', 'STILL')} / "
              f"{final_l2.get('direction_lock_state', 'UNLOCKED')} / "
              f"{final_l2.get('direction_global_effective', 'UNDECIDED')}")
        print(f"  L2 Rotations:    {final_l2.get('rotations', 0):.2f}")
        print("=" * 65)
        
        if args.log:
            print(f"\n[i] Log saved to: live_encoder_*.jsonl")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
