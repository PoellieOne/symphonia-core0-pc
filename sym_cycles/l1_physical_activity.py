#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l1_physical_activity.py — L1 PhysicalActivity Layer v0.2 (Encoder-Aware)

S02.HandEncoder-Awareness v0.1 implementatie.

Changelog v0.2:
- NIEUW: 5-state model: STILL / FEELING / SCRAPE / DISPLACEMENT / MOVING
- NIEUW: θ̂ (theta_hat) virtuele hoek integrator als displacement gate
- NIEUW: activity_score en disp_score metrics
- NIEUW: Doorlussen van L2 direction/lock info
- NIEUW: encoder_conf samengesteld kwaliteitsgetal
- BEHOUDEN: Backwards-compatible L1State enum (oude states mappen naar nieuwe)

Kernprincipe:
- Activiteit (tactiel): events / prikkels → FEELING / SCRAPE
- Displacement (kinematica): netto θ̂ verplaatsing → DISPLACEMENT / MOVING
- L1 wijzigt NIETS aan L2 (cycles, compass, lock, etc.)

States:
- STILL:        Activiteit laag, displacement laag
- FEELING:      Activiteit aanwezig, displacement ~0 (touch)
- SCRAPE:       Activiteit hoog, displacement ~0 (edge oscillatie)
- DISPLACEMENT: Netto displacement, direction nog onzeker
- MOVING:       Displacement + direction stabiel (lock soft/locked)

Gebruik:
    from l1_physical_activity import L1PhysicalActivity, L1State, L1Config
    
    l1 = L1PhysicalActivity(config=L1Config())
    
    # Bij elke RealtimeSnapshot van L2:
    l1_snap = l1.update(
        wall_time=time.time(),
        cycles_physical_total=snap.total_cycles_physical,
        events_this_batch=batch_event_count,  # Raw EVENT24 frames!
        rotations=snap.rotations,             # Van L2 (optioneel)
        direction_conf=snap.compass.confidence,
        lock_state=snap.direction_lock_state,
        direction_effective=snap.direction_global_effective,
    )
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum
from collections import deque


class L1State(Enum):
    """
    L1 PhysicalActivity states (v0.2 encoder-aware).
    
    State hierarchy:
        STILL < FEELING < SCRAPE < DISPLACEMENT < MOVING
    """
    STILL = "STILL"              # Geen activiteit, geen displacement
    FEELING = "FEELING"          # Lichte activiteit, geen displacement (touch)
    SCRAPE = "SCRAPE"            # Hoge activiteit, geen displacement (edge oscillatie)
    DISPLACEMENT = "DISPLACEMENT"  # Netto displacement, direction onzeker
    MOVING = "MOVING"            # Displacement + stabiele direction
    
    # Legacy compatibility
    IDLE = "STILL"               # Alias
    TENSION = "SCRAPE"           # Alias
    MOVING_RAW = "DISPLACEMENT"  # Alias


@dataclass
class L1Config:
    """
    Configuratie voor L1 PhysicalActivity detectie (v0.2 encoder-aware).
    
    Thresholds:
    - A0, A1: activity score drempels (events/window)
    - D0: minimum |Δθ̂| per window voor displacement
    - C0: minimum direction_conf voor MOVING
    """
    # Gap threshold: na deze tijd zonder cycles → STILL
    gap_ms: float = 500.0
    
    # Activity thresholds (events per update window)
    activity_threshold_low: float = 1.0    # A0: STILL → FEELING
    activity_threshold_high: float = 5.0   # A1: FEELING → SCRAPE
    
    # Displacement threshold (rotations per update window)
    displacement_threshold: float = 0.01   # D0: min |Δθ̂| voor DISPLACEMENT
    
    # Direction confidence threshold
    direction_conf_threshold: float = 0.5  # C0: min conf voor MOVING
    
    # Lock states die MOVING triggeren
    lock_states_for_moving: tuple = ("SOFT_LOCK", "LOCKED")
    
    # Window voor activity/displacement berekening
    window_ms: float = 100.0
    
    # Cycles per rotation (van L2, default 12)
    cycles_per_rot: float = 12.0
    
    # Tension tracking (legacy compatibility)
    tension_window_ms: float = 300.0
    tension_min_events: int = 3


@dataclass
class L1Snapshot:
    """
    Snapshot van L1 state (v0.2 encoder-aware).
    
    Bevat encoder-achtige observables voor live display en logging.
    """
    # Primaire state
    state: L1State
    
    # Virtuele hoek θ̂ (encoder-achtig)
    theta_hat_rot: float = 0.0          # Cumulatieve rotaties (van L2 of berekend)
    delta_theta_rot: float = 0.0        # Δθ̂ per window
    
    # Activity metrics
    activity_score: float = 0.0         # Events per seconde (of per window)
    disp_score: float = 0.0             # |Δθ̂| per window
    
    # Doorgeluste L2 info
    direction_effective: str = "UNDECIDED"
    direction_conf: float = 0.0
    lock_state: str = "UNLOCKED"
    
    # Encoder confidence (samengesteld)
    encoder_conf: float = 0.0
    
    # Timing
    t_last_cycle: Optional[float] = None
    t_last_event: Optional[float] = None
    gap_since_cycle_ms: float = float('inf')
    gap_since_event_ms: float = float('inf')
    
    # Counters
    total_cycles: float = 0.0
    total_events: int = 0
    delta_cycles: float = 0.0
    delta_events: int = 0
    
    # Tension tracking (legacy)
    events_without_cycles: int = 0
    tension_start_t: Optional[float] = None


class L1PhysicalActivity:
    """
    L1 PhysicalActivity Layer v0.2 (Encoder-Aware).
    
    Implementeert passief "encoder-achtig" bewustzijn bij handmatige rotorbeweging.
    Scheidt activiteit (tactiel) van displacement (kinematica).
    
    Wijzigt NIETS aan L2 — alleen interpretatie en state tracking.
    """
    
    def __init__(self, config: L1Config = None):
        self.config = config or L1Config()
        
        # Current state
        self._state: L1State = L1State.STILL
        
        # Virtuele hoek θ̂ integrator
        self._theta_hat_rot: float = 0.0
        self._prev_theta_hat_rot: float = 0.0
        
        # Timing
        self._t_last_cycle: Optional[float] = None
        self._t_last_event: Optional[float] = None
        self._t_last_update: Optional[float] = None
        self._tension_start_t: Optional[float] = None
        
        # Counters
        self._prev_cycles_total: float = 0.0
        self._prev_events_total: int = 0
        self._events_without_cycles: int = 0
        
        # Activity tracking (sliding window)
        self._event_times: deque = deque(maxlen=100)
        
        # L2 doorgeluste state
        self._direction_effective: str = "UNDECIDED"
        self._direction_conf: float = 0.0
        self._lock_state: str = "UNLOCKED"
    
    @property
    def state(self) -> L1State:
        """Huidige L1 state."""
        return self._state
    
    @property
    def theta_hat_rot(self) -> float:
        """Huidige virtuele hoek in rotaties."""
        return self._theta_hat_rot
    
    def update(
        self,
        wall_time: float,
        cycles_physical_total: float,
        events_this_batch: int = 0,
        events_total: int = None,
        # Optionele L2 doorlus
        rotations: float = None,
        direction_conf: float = None,
        lock_state: str = None,
        direction_effective: str = None,
    ) -> L1Snapshot:
        """
        Update L1 state op basis van nieuwe data.
        
        Args:
            wall_time: Huidige tijd (seconds since epoch)
            cycles_physical_total: Totaal aantal cycles (monotoon stijgend)
            events_this_batch: Aantal EVENT24 frames in deze batch
            events_total: Totaal aantal events (alternatief voor batch)
            rotations: Rotaties van L2 snapshot (optioneel, preferred)
            direction_conf: Direction confidence van L2 compass
            lock_state: Lock state van L2 ("UNLOCKED", "SOFT_LOCK", "LOCKED")
            direction_effective: Direction van L2 ("CW", "CCW", "UNDECIDED")
        
        Returns:
            L1Snapshot met huidige state en encoder-achtige metrics
        """
        cfg = self.config
        
        # === Delta berekeningen ===
        delta_cycles = cycles_physical_total - self._prev_cycles_total
        
        if events_total is not None:
            delta_events = events_total - self._prev_events_total
            self._prev_events_total = events_total
        else:
            delta_events = events_this_batch
            self._prev_events_total += events_this_batch
        
        self._prev_cycles_total = cycles_physical_total
        
        # === Virtuele hoek θ̂ update ===
        self._prev_theta_hat_rot = self._theta_hat_rot
        
        if rotations is not None:
            # Prefer L2 rotations als beschikbaar
            self._theta_hat_rot = rotations
        else:
            # Fallback: integreer op cycles
            self._theta_hat_rot = cycles_physical_total / cfg.cycles_per_rot
        
        delta_theta_rot = self._theta_hat_rot - self._prev_theta_hat_rot
        
        # === Timing update ===
        dt_s = 0.0
        if self._t_last_update is not None:
            dt_s = wall_time - self._t_last_update
        self._t_last_update = wall_time
        
        if delta_cycles > 0:
            self._t_last_cycle = wall_time
            self._events_without_cycles = 0
            self._tension_start_t = None
        
        if delta_events > 0:
            self._t_last_event = wall_time
            # Track event times voor activity score
            for _ in range(delta_events):
                self._event_times.append(wall_time)
            
            if delta_cycles == 0:
                self._events_without_cycles += delta_events
                if self._tension_start_t is None:
                    self._tension_start_t = wall_time
        
        # === L2 doorlus ===
        if direction_conf is not None:
            self._direction_conf = direction_conf
        if lock_state is not None:
            self._lock_state = lock_state
        if direction_effective is not None:
            self._direction_effective = direction_effective
        
        # === Bereken metrics ===
        
        # Gap berekening
        gap_since_cycle_ms = float('inf')
        if self._t_last_cycle is not None:
            gap_since_cycle_ms = (wall_time - self._t_last_cycle) * 1000.0
        
        gap_since_event_ms = float('inf')
        if self._t_last_event is not None:
            gap_since_event_ms = (wall_time - self._t_last_event) * 1000.0
        
        # Activity score: events per seconde (rolling window van 1s)
        recent_events = [t for t in self._event_times if wall_time - t < 1.0]
        activity_score = float(len(recent_events))
        
        # Displacement score: |Δθ̂| per window
        disp_score = abs(delta_theta_rot)
        
        # Encoder confidence (samengesteld)
        encoder_conf = self._compute_encoder_conf(
            activity_score=activity_score,
            disp_score=disp_score,
            direction_conf=self._direction_conf,
            lock_state=self._lock_state,
        )
        
        # === State machine ===
        self._state = self._compute_state(
            wall_time=wall_time,
            activity_score=activity_score,
            disp_score=disp_score,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
        )
        
        return L1Snapshot(
            state=self._state,
            theta_hat_rot=self._theta_hat_rot,
            delta_theta_rot=delta_theta_rot,
            activity_score=activity_score,
            disp_score=disp_score,
            direction_effective=self._direction_effective,
            direction_conf=self._direction_conf,
            lock_state=self._lock_state,
            encoder_conf=encoder_conf,
            t_last_cycle=self._t_last_cycle,
            t_last_event=self._t_last_event,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
            total_cycles=cycles_physical_total,
            total_events=self._prev_events_total,
            delta_cycles=delta_cycles,
            delta_events=delta_events,
            events_without_cycles=self._events_without_cycles,
            tension_start_t=self._tension_start_t,
        )
    
    def _compute_state(
        self,
        wall_time: float,
        activity_score: float,
        disp_score: float,
        gap_since_cycle_ms: float,
        gap_since_event_ms: float,
    ) -> L1State:
        """
        Bereken L1 state op basis van activity en displacement scores.
        
        Decision tree:
            disp_score >= D0?
                YES → direction stabiel? → MOVING
                      ELSE → DISPLACEMENT
                NO  → activity_score >= A1? → SCRAPE
                      activity_score >= A0? → FEELING
                      ELSE → STILL
        """
        cfg = self.config
        
        # Check voor gap timeout → STILL
        if gap_since_cycle_ms >= cfg.gap_ms and gap_since_event_ms >= cfg.gap_ms:
            return L1State.STILL
        
        # === Displacement gate ===
        has_displacement = disp_score >= cfg.displacement_threshold
        
        if has_displacement:
            # Check direction stabiliteit
            direction_stable = (
                self._direction_conf >= cfg.direction_conf_threshold or
                self._lock_state in cfg.lock_states_for_moving
            )
            
            if direction_stable:
                return L1State.MOVING
            else:
                return L1State.DISPLACEMENT
        
        # === Geen displacement, check activiteit ===
        if activity_score >= cfg.activity_threshold_high:
            return L1State.SCRAPE
        elif activity_score >= cfg.activity_threshold_low:
            return L1State.FEELING
        else:
            return L1State.STILL
    
    def _compute_encoder_conf(
        self,
        activity_score: float,
        disp_score: float,
        direction_conf: float,
        lock_state: str,
    ) -> float:
        """
        Bereken samengesteld encoder confidence getal.
        
        Combineert:
        - Activity indicator (is er sensing?)
        - Displacement indicator (is er beweging?)
        - Direction confidence (is richting bekend?)
        - Lock state (is richting stabiel?)
        """
        cfg = self.config
        
        # Activity component: 0-1 genormaliseerd
        activity_norm = min(1.0, activity_score / max(cfg.activity_threshold_high * 2, 1.0))
        
        # Displacement component: 0-1 genormaliseerd
        disp_norm = min(1.0, disp_score / max(cfg.displacement_threshold * 10, 0.01))
        
        # Lock component
        if lock_state == "LOCKED":
            lock_norm = 1.0
        elif lock_state == "SOFT_LOCK":
            lock_norm = 0.7
        else:
            lock_norm = 0.3
        
        # Direction confidence (al 0-1)
        dir_norm = direction_conf
        
        # Gewogen combinatie
        # Displacement en lock zijn belangrijker dan activiteit alleen
        encoder_conf = (
            0.2 * activity_norm +
            0.3 * disp_norm +
            0.25 * lock_norm +
            0.25 * dir_norm
        )
        
        return min(1.0, max(0.0, encoder_conf))
    
    def reset(self):
        """Reset L1 state naar STILL."""
        self._state = L1State.STILL
        self._theta_hat_rot = 0.0
        self._prev_theta_hat_rot = 0.0
        self._t_last_cycle = None
        self._t_last_event = None
        self._t_last_update = None
        self._tension_start_t = None
        self._prev_cycles_total = 0.0
        self._prev_events_total = 0
        self._events_without_cycles = 0
        self._event_times.clear()
        self._direction_effective = "UNDECIDED"
        self._direction_conf = 0.0
        self._lock_state = "UNLOCKED"
    
    def get_snapshot(self, wall_time: float) -> L1Snapshot:
        """Get current snapshot zonder update."""
        gap_since_cycle_ms = float('inf')
        if self._t_last_cycle is not None:
            gap_since_cycle_ms = (wall_time - self._t_last_cycle) * 1000.0
        
        gap_since_event_ms = float('inf')
        if self._t_last_event is not None:
            gap_since_event_ms = (wall_time - self._t_last_event) * 1000.0
        
        recent_events = [t for t in self._event_times if wall_time - t < 1.0]
        activity_score = float(len(recent_events))
        
        encoder_conf = self._compute_encoder_conf(
            activity_score=activity_score,
            disp_score=0.0,
            direction_conf=self._direction_conf,
            lock_state=self._lock_state,
        )
        
        return L1Snapshot(
            state=self._state,
            theta_hat_rot=self._theta_hat_rot,
            delta_theta_rot=0.0,
            activity_score=activity_score,
            disp_score=0.0,
            direction_effective=self._direction_effective,
            direction_conf=self._direction_conf,
            lock_state=self._lock_state,
            encoder_conf=encoder_conf,
            t_last_cycle=self._t_last_cycle,
            t_last_event=self._t_last_event,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
            total_cycles=self._prev_cycles_total,
            total_events=self._prev_events_total,
            delta_cycles=0.0,
            delta_events=0,
            events_without_cycles=self._events_without_cycles,
            tension_start_t=self._tension_start_t,
        )


# === Preset Configs ===

L1_CONFIG_HUMAN = L1Config(
    gap_ms=500.0,
    activity_threshold_low=1.0,
    activity_threshold_high=5.0,
    displacement_threshold=0.01,
    direction_conf_threshold=0.5,
    cycles_per_rot=12.0,
)

L1_CONFIG_BENCH = L1Config(
    gap_ms=800.0,
    activity_threshold_low=2.0,
    activity_threshold_high=8.0,
    displacement_threshold=0.02,
    direction_conf_threshold=0.6,
    cycles_per_rot=12.0,
)

L1_CONFIG_PRODUCTION = L1Config(
    gap_ms=1000.0,
    activity_threshold_low=2.0,
    activity_threshold_high=10.0,
    displacement_threshold=0.02,
    direction_conf_threshold=0.7,
    cycles_per_rot=12.0,
)


# === Legacy compatibility aliases ===

# Old 3-state to new 5-state mapping:
# IDLE       → STILL
# TENSION    → SCRAPE (or FEELING depending on intensity)
# MOVING_RAW → DISPLACEMENT (or MOVING if direction stable)
