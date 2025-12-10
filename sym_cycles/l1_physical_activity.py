#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l1_physical_activity.py — L1 PhysicalActivity Layer

Een expliciete interpretatielaag BOVENOP de bestaande RealtimePipeline v1.9 (L2).

Kernprincipe:
- L1 interpreteert ALLEEN of er fysieke activiteit is
- L1 wijzigt NIETS aan L2 (cycles, compass, lock, etc.)
- L1 is bruikbaar voor zowel live als offline replay

States:
- IDLE:       Geen cycles en geen relevante events in recent tijdvenster
- TENSION:    Event-activiteit maar geen cyclische progressie (edge-scraping)
- MOVING_RAW: Echte cyclische beweging (cycles_physical > 0)

Gebruik:
    from l1_physical_activity import L1PhysicalActivity, L1State
    
    l1 = L1PhysicalActivity(gap_ms=500, tension_window_ms=300)
    
    # Bij elke RealtimeSnapshot van L2:
    l1_state = l1.update(
        wall_time=time.time(),
        cycles_physical_total=snap.total_cycles_physical,
        events_this_batch=len(snap.cycles_emitted),
    )
    
    print(f"L1: {l1_state.state}")  # IDLE, TENSION, MOVING_RAW
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class L1State(Enum):
    """L1 PhysicalActivity states."""
    IDLE = "IDLE"
    TENSION = "TENSION"
    MOVING_RAW = "MOVING_RAW"


@dataclass
class L1Config:
    """Configuratie voor L1 PhysicalActivity detectie."""
    # Gap threshold: na deze tijd zonder cycles → IDLE
    gap_ms: float = 500.0
    
    # Tension window: events zonder cycles voor deze duur → TENSION
    tension_window_ms: float = 300.0
    
    # Minimum events in tension window om TENSION te triggeren
    tension_min_events: int = 3


@dataclass
class L1Snapshot:
    """Snapshot van L1 state."""
    state: L1State
    
    # Timing
    t_last_cycle: Optional[float] = None      # Wall time van laatste cycle
    t_last_event: Optional[float] = None      # Wall time van laatste event
    gap_since_cycle_ms: float = float('inf')  # Ms sinds laatste cycle
    gap_since_event_ms: float = float('inf')  # Ms sinds laatste event
    
    # Counters (monotoon, voor delta berekening)
    total_cycles: float = 0.0
    total_events: int = 0
    
    # Delta sinds vorige update
    delta_cycles: float = 0.0
    delta_events: int = 0
    
    # Tension tracking
    events_without_cycles: int = 0  # Events sinds laatste cycle
    tension_start_t: Optional[float] = None


class L1PhysicalActivity:
    """
    L1 PhysicalActivity Layer.
    
    Interpreteert fysieke activiteit op basis van L2 RealtimePipeline output.
    Wijzigt NIETS aan L2 — alleen interpretatie en state tracking.
    """
    
    def __init__(self, config: L1Config = None):
        self.config = config or L1Config()
        
        # Current state
        self._state: L1State = L1State.IDLE
        
        # Timing
        self._t_last_cycle: Optional[float] = None
        self._t_last_event: Optional[float] = None
        self._tension_start_t: Optional[float] = None
        
        # Counters (voor delta berekening)
        self._prev_cycles_total: float = 0.0
        self._prev_events_total: int = 0
        
        # Tension tracking
        self._events_without_cycles: int = 0
    
    @property
    def state(self) -> L1State:
        """Huidige L1 state."""
        return self._state
    
    def update(
        self,
        wall_time: float,
        cycles_physical_total: float,
        events_this_batch: int = 0,
        events_total: int = None,
    ) -> L1Snapshot:
        """
        Update L1 state op basis van nieuwe L2 data.
        
        Args:
            wall_time: Huidige tijd (seconds since epoch)
            cycles_physical_total: Totaal aantal cycles (monotoon stijgend)
            events_this_batch: Aantal events in deze batch (optioneel)
            events_total: Totaal aantal events (optioneel, alternatief voor batch)
        
        Returns:
            L1Snapshot met huidige state en metrics
        """
        # Bereken deltas
        delta_cycles = cycles_physical_total - self._prev_cycles_total
        
        if events_total is not None:
            delta_events = events_total - self._prev_events_total
            self._prev_events_total = events_total
        else:
            delta_events = events_this_batch
            self._prev_events_total += events_this_batch
        
        self._prev_cycles_total = cycles_physical_total
        
        # Update timing
        if delta_cycles > 0:
            self._t_last_cycle = wall_time
            self._events_without_cycles = 0
            self._tension_start_t = None
        
        if delta_events > 0:
            self._t_last_event = wall_time
            if delta_cycles == 0:
                self._events_without_cycles += delta_events
                if self._tension_start_t is None:
                    self._tension_start_t = wall_time
        
        # Bereken gaps
        gap_since_cycle_ms = float('inf')
        if self._t_last_cycle is not None:
            gap_since_cycle_ms = (wall_time - self._t_last_cycle) * 1000.0
        
        gap_since_event_ms = float('inf')
        if self._t_last_event is not None:
            gap_since_event_ms = (wall_time - self._t_last_event) * 1000.0
        
        # State machine
        prev_state = self._state
        self._state = self._compute_state(
            wall_time=wall_time,
            delta_cycles=delta_cycles,
            delta_events=delta_events,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
        )
        
        return L1Snapshot(
            state=self._state,
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
        delta_cycles: float,
        delta_events: int,
        gap_since_cycle_ms: float,
        gap_since_event_ms: float,
    ) -> L1State:
        """
        Bereken nieuwe L1 state op basis van huidige condities.
        """
        cfg = self.config
        
        # MOVING_RAW: er zijn nieuwe cycles
        if delta_cycles > 0:
            return L1State.MOVING_RAW
        
        # Nog steeds MOVING_RAW als gap klein is
        if gap_since_cycle_ms < cfg.gap_ms:
            # Check of we in TENSION moeten vanwege events zonder cycles
            if self._events_without_cycles >= cfg.tension_min_events:
                tension_duration_ms = 0
                if self._tension_start_t is not None:
                    tension_duration_ms = (wall_time - self._tension_start_t) * 1000.0
                
                if tension_duration_ms >= cfg.tension_window_ms:
                    return L1State.TENSION
            
            return L1State.MOVING_RAW
        
        # TENSION: events maar geen cycles, en gap > gap_ms
        if delta_events > 0 or gap_since_event_ms < cfg.tension_window_ms:
            if self._events_without_cycles >= cfg.tension_min_events:
                return L1State.TENSION
        
        # TENSION: geen nieuwe events maar nog in tension window
        if self._tension_start_t is not None:
            tension_duration_ms = (wall_time - self._tension_start_t) * 1000.0
            if tension_duration_ms < cfg.gap_ms:  # Tension timeout = gap_ms
                if gap_since_event_ms < cfg.gap_ms:
                    return L1State.TENSION
        
        # IDLE: geen cycles, geen relevante events
        return L1State.IDLE
    
    def reset(self):
        """Reset L1 state naar IDLE."""
        self._state = L1State.IDLE
        self._t_last_cycle = None
        self._t_last_event = None
        self._tension_start_t = None
        self._prev_cycles_total = 0.0
        self._prev_events_total = 0
        self._events_without_cycles = 0
    
    def get_snapshot(self, wall_time: float) -> L1Snapshot:
        """Get current snapshot zonder update."""
        gap_since_cycle_ms = float('inf')
        if self._t_last_cycle is not None:
            gap_since_cycle_ms = (wall_time - self._t_last_cycle) * 1000.0
        
        gap_since_event_ms = float('inf')
        if self._t_last_event is not None:
            gap_since_event_ms = (wall_time - self._t_last_event) * 1000.0
        
        return L1Snapshot(
            state=self._state,
            t_last_cycle=self._t_last_cycle,
            t_last_event=self._t_last_event,
            gap_since_cycle_ms=gap_since_cycle_ms,
            gap_since_event_ms=gap_since_event_ms,
            total_cycles=self._prev_cycles_total,
            total_events=self._prev_events_total,
            delta_cycles=0,
            delta_events=0,
            events_without_cycles=self._events_without_cycles,
            tension_start_t=self._tension_start_t,
        )


# === Preset Configs ===

L1_CONFIG_HUMAN = L1Config(
    gap_ms=500.0,
    tension_window_ms=300.0,
    tension_min_events=3,
)

L1_CONFIG_BENCH = L1Config(
    gap_ms=800.0,
    tension_window_ms=400.0,
    tension_min_events=5,
)

L1_CONFIG_PRODUCTION = L1Config(
    gap_ms=1000.0,
    tension_window_ms=500.0,
    tension_min_events=5,
)
