#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l1_physical_activity.py — L1 PhysicalActivity Layer v0.4 (Observability)

S02.HandEncoder-Awareness — Patch D2: Reason codes + threshold export.

Changelog v0.4:
- PATCH D2: reason codes bij state transitions
- PATCH D2: thresholds dict in snapshot voor debug
- FIX: reason explains WHY state was chosen
- Alle v0.3 patches behouden (A/B/C)

Reason codes:
- STILL_GAP_TIMEOUT: geen events/cycles binnen gap_ms
- STILL_LOW_ACTIVITY: activity < A0 en disp < D0
- FEELING_ACTIVITY_NO_DISP: activity >= A0, disp < D0
- SCRAPE_HIGH_ACTIVITY: activity >= A1, disp < D0
- DISP_ABOVE_D0: |Δθ| >= D0, direction unstable
- MOVING_STABLE_DIR: |Δθ| >= D0, direction stable (conf >= C0)
- MOVING_LOCKED: |Δθ| >= D0, lock in SOFT_LOCK/LOCKED
- HARD_RESET_GAP: dt > hard_reset_s triggered
- DECAY_ACTIVE: encoder_conf decaying (no state change)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from enum import Enum
import math


class L1State(Enum):
    """L1 PhysicalActivity states."""
    STILL = "STILL"
    FEELING = "FEELING"
    SCRAPE = "SCRAPE"
    DISPLACEMENT = "DISPLACEMENT"
    MOVING = "MOVING"


class L1Reason(Enum):
    """Reason codes for L1 state (Patch D2)."""
    STILL_GAP_TIMEOUT = "STILL_GAP_TIMEOUT"
    STILL_LOW_ACTIVITY = "STILL_LOW_ACTIVITY"
    FEELING_ACTIVITY_NO_DISP = "FEELING_ACTIVITY_NO_DISP"
    SCRAPE_HIGH_ACTIVITY = "SCRAPE_HIGH_ACTIVITY"
    DISP_ABOVE_D0 = "DISP_ABOVE_D0"
    MOVING_STABLE_DIR = "MOVING_STABLE_DIR"
    MOVING_LOCKED = "MOVING_LOCKED"
    HARD_RESET_GAP = "HARD_RESET_GAP"
    DECAY_ACTIVE = "DECAY_ACTIVE"
    INIT = "INIT"


@dataclass
class L1Config:
    """Configuratie voor L1 PhysicalActivity (v0.4)."""
    # Gap threshold
    gap_ms: float = 500.0
    
    # Activity thresholds
    activity_threshold_low: float = 1.0    # A0
    activity_threshold_high: float = 5.0   # A1
    
    # Displacement threshold
    displacement_threshold: float = 0.005  # D0 (rotations)
    
    # Direction thresholds
    direction_conf_threshold: float = 0.5  # C0
    lock_states_for_moving: tuple = ("SOFT_LOCK", "LOCKED")
    
    # Cycles per rotation
    cycles_per_rot: float = 12.0
    
    # Time decay (Patch B)
    encoder_tau_s: float = 0.6
    hard_reset_s: float = 1.5
    activity_decay_rate: float = 5.0


@dataclass
class L1Snapshot:
    """
    Snapshot van L1 state (v0.4 met reason codes).
    
    Patch D2: reason + thresholds voor debug.
    """
    state: L1State
    reason: L1Reason  # WHY this state was chosen
    
    # θ̂ (uit cycles)
    theta_hat_rot: float = 0.0
    theta_hat_deg: float = 0.0      # Patch D1: degrees
    delta_theta_rot: float = 0.0
    delta_theta_deg: float = 0.0    # Patch D1: degrees
    
    # Activity
    activity_score: float = 0.0
    disp_score: float = 0.0
    
    # L2 doorlus
    direction_effective: str = "UNDECIDED"
    direction_conf: float = 0.0
    lock_state: str = "UNLOCKED"
    
    # Encoder confidence
    encoder_conf: float = 0.0
    
    # Timing
    dt_s: float = 0.0
    t_last_cycle: Optional[float] = None
    t_last_event: Optional[float] = None
    gap_since_cycle_ms: float = float('inf')
    gap_since_event_ms: float = float('inf')
    
    # Counters
    total_cycles: float = 0.0
    delta_cycles: float = 0.0       # Patch D1
    total_events: int = 0
    delta_events: int = 0
    events_without_cycles: int = 0
    
    # Thresholds (Patch D2: voor debug)
    thresholds: Dict[str, Any] = field(default_factory=dict)


class L1PhysicalActivity:
    """
    L1 PhysicalActivity Layer v0.4 (Observability).
    
    Patch D2: Every state decision comes with a reason code.
    """
    
    def __init__(self, config: L1Config = None):
        self.config = config or L1Config()
        
        self._state: L1State = L1State.STILL
        self._reason: L1Reason = L1Reason.INIT
        
        # θ̂
        self._theta_hat_rot: float = 0.0
        self._prev_theta_hat_rot: float = 0.0
        
        # Timing
        self._t_last_update: Optional[float] = None
        self._t_last_cycle: Optional[float] = None
        self._t_last_event: Optional[float] = None
        
        # Counters
        self._prev_cycles_total: float = 0.0
        self._total_events: int = 0
        self._events_without_cycles: int = 0
        
        # Activity + confidence
        self._activity_score: float = 0.0
        self._encoder_conf: float = 0.0
        
        # L2 doorlus
        self._direction_effective: str = "UNDECIDED"
        self._direction_conf: float = 0.0
        self._lock_state: str = "UNLOCKED"
    
    @property
    def state(self) -> L1State:
        return self._state
    
    @property
    def reason(self) -> L1Reason:
        return self._reason
    
    def update(
        self,
        wall_time: float,
        cycles_physical_total: float,
        events_this_batch: int = 0,
        direction_conf: float = None,
        lock_state: str = None,
        direction_effective: str = None,
        rotations: float = None,  # Ignored
    ) -> L1Snapshot:
        """Update L1 state met reason tracking."""
        cfg = self.config
        
        # === Timing ===
        dt_s = 0.0
        if self._t_last_update is not None:
            dt_s = wall_time - self._t_last_update
        self._t_last_update = wall_time
        
        # === Hard reset check (Patch B2) ===
        if dt_s > cfg.hard_reset_s:
            self._hard_reset()
            self._reason = L1Reason.HARD_RESET_GAP
            dt_s = 0.0
        
        # === Deltas ===
        delta_cycles = cycles_physical_total - self._prev_cycles_total
        self._prev_cycles_total = cycles_physical_total
        
        delta_events = events_this_batch
        self._total_events += events_this_batch
        
        # === θ̂ uit cycles (Patch A) ===
        self._prev_theta_hat_rot = self._theta_hat_rot
        self._theta_hat_rot = cycles_physical_total / cfg.cycles_per_rot
        delta_theta_rot = self._theta_hat_rot - self._prev_theta_hat_rot
        
        # Degrees (Patch D1)
        theta_hat_deg = (self._theta_hat_rot * 360.0) % 360.0
        delta_theta_deg = delta_theta_rot * 360.0
        
        # === Timing updates ===
        if delta_cycles > 0:
            self._t_last_cycle = wall_time
            self._events_without_cycles = 0
        
        if delta_events > 0:
            self._t_last_event = wall_time
            if delta_cycles == 0:
                self._events_without_cycles += delta_events
        
        # === L2 doorlus ===
        if direction_conf is not None:
            self._direction_conf = direction_conf
        if lock_state is not None:
            self._lock_state = lock_state
        if direction_effective is not None:
            self._direction_effective = direction_effective
        
        # === Gaps ===
        gap_since_cycle_ms = float('inf')
        if self._t_last_cycle is not None:
            gap_since_cycle_ms = (wall_time - self._t_last_cycle) * 1000.0
        
        gap_since_event_ms = float('inf')
        if self._t_last_event is not None:
            gap_since_event_ms = (wall_time - self._t_last_event) * 1000.0
        
        # === Activity decay (Patch B1) ===
        if dt_s > 0:
            decay_factor = math.exp(-dt_s * cfg.activity_decay_rate)
            self._activity_score *= decay_factor
        self._activity_score += delta_events
        
        # === Displacement score ===
        disp_score = abs(delta_theta_rot)
        
        # === Encoder conf decay (Patch B1) ===
        if dt_s > 0:
            decay_factor = math.exp(-dt_s / cfg.encoder_tau_s)
            self._encoder_conf *= decay_factor
        
        # Boost
        if delta_cycles > 0:
            self._encoder_conf = min(1.0, self._encoder_conf + 0.15)
        elif delta_events > 0:
            self._encoder_conf = min(1.0, self._encoder_conf + 0.05)
        
        if self._lock_state == "LOCKED":
            self._encoder_conf = min(1.0, self._encoder_conf + 0.1 * dt_s)
        
        self._encoder_conf = max(0.0, min(1.0, self._encoder_conf))
        
        # === State machine met reason (Patch D2) ===
        self._state, self._reason = self._compute_state_with_reason(
            activity_score=self._activity_score,
            disp_score=disp_score,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
        )
        
        # === Thresholds dict (Patch D2) ===
        thresholds = {
            "A0": cfg.activity_threshold_low,
            "A1": cfg.activity_threshold_high,
            "D0": cfg.displacement_threshold,
            "D0_deg": cfg.displacement_threshold * 360.0,
            "C0": cfg.direction_conf_threshold,
            "gap_ms": cfg.gap_ms,
            "tau_s": cfg.encoder_tau_s,
            "hard_reset_s": cfg.hard_reset_s,
        }
        
        return L1Snapshot(
            state=self._state,
            reason=self._reason,
            theta_hat_rot=self._theta_hat_rot,
            theta_hat_deg=theta_hat_deg,
            delta_theta_rot=delta_theta_rot,
            delta_theta_deg=delta_theta_deg,
            activity_score=self._activity_score,
            disp_score=disp_score,
            direction_effective=self._direction_effective,
            direction_conf=self._direction_conf,
            lock_state=self._lock_state,
            encoder_conf=self._encoder_conf,
            dt_s=dt_s,
            t_last_cycle=self._t_last_cycle,
            t_last_event=self._t_last_event,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
            total_cycles=cycles_physical_total,
            delta_cycles=delta_cycles,
            total_events=self._total_events,
            delta_events=delta_events,
            events_without_cycles=self._events_without_cycles,
            thresholds=thresholds,
        )
    
    def _compute_state_with_reason(
        self,
        activity_score: float,
        disp_score: float,
        gap_since_cycle_ms: float,
        gap_since_event_ms: float,
    ) -> tuple[L1State, L1Reason]:
        """
        State machine met reason codes (Patch D2).
        
        Returns (state, reason) tuple.
        """
        cfg = self.config
        
        # 1) Gap timeout → STILL
        if gap_since_cycle_ms >= cfg.gap_ms and gap_since_event_ms >= cfg.gap_ms:
            return L1State.STILL, L1Reason.STILL_GAP_TIMEOUT
        
        # 2) Low activity, no displacement → STILL
        if activity_score < cfg.activity_threshold_low and disp_score < cfg.displacement_threshold:
            return L1State.STILL, L1Reason.STILL_LOW_ACTIVITY
        
        # 3) Displacement gate
        has_displacement = disp_score >= cfg.displacement_threshold
        
        if has_displacement:
            # Check direction stability
            if self._lock_state in cfg.lock_states_for_moving:
                return L1State.MOVING, L1Reason.MOVING_LOCKED
            elif self._direction_conf >= cfg.direction_conf_threshold:
                return L1State.MOVING, L1Reason.MOVING_STABLE_DIR
            else:
                return L1State.DISPLACEMENT, L1Reason.DISP_ABOVE_D0
        
        # 4) Activity only (no displacement)
        if activity_score >= cfg.activity_threshold_high:
            return L1State.SCRAPE, L1Reason.SCRAPE_HIGH_ACTIVITY
        elif activity_score >= cfg.activity_threshold_low:
            return L1State.FEELING, L1Reason.FEELING_ACTIVITY_NO_DISP
        
        return L1State.STILL, L1Reason.STILL_LOW_ACTIVITY
    
    def _hard_reset(self):
        """Hard reset (Patch B2)."""
        self._state = L1State.STILL
        self._encoder_conf = 0.0
        self._activity_score = 0.0
        self._events_without_cycles = 0
    
    def reset(self):
        """Full reset."""
        self._state = L1State.STILL
        self._reason = L1Reason.INIT
        self._theta_hat_rot = 0.0
        self._prev_theta_hat_rot = 0.0
        self._t_last_update = None
        self._t_last_cycle = None
        self._t_last_event = None
        self._prev_cycles_total = 0.0
        self._total_events = 0
        self._events_without_cycles = 0
        self._activity_score = 0.0
        self._encoder_conf = 0.0
        self._direction_effective = "UNDECIDED"
        self._direction_conf = 0.0
        self._lock_state = "UNLOCKED"


# === Preset Configs ===

L1_CONFIG_HUMAN = L1Config(
    gap_ms=500.0,
    activity_threshold_low=1.0,
    activity_threshold_high=5.0,
    displacement_threshold=0.005,
    direction_conf_threshold=0.5,
    cycles_per_rot=12.0,
    encoder_tau_s=0.6,
    hard_reset_s=1.5,
    activity_decay_rate=5.0,
)

L1_CONFIG_BENCH = L1Config(
    gap_ms=800.0,
    activity_threshold_low=2.0,
    activity_threshold_high=8.0,
    displacement_threshold=0.01,
    direction_conf_threshold=0.6,
    cycles_per_rot=12.0,
    encoder_tau_s=0.8,
    hard_reset_s=2.0,
    activity_decay_rate=3.0,
)
