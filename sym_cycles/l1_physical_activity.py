#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l1_physical_activity.py — L1 PhysicalActivity Layer v1.1 (OriginTracker v0.4.5)

S02.OriginTracker v0.4.5 — Low-Event Robustness via MDI Modes A/B/C.

Changelog v0.4.5:
- NEW: mdi_mode A/B/C for different sensitivity profiles
  - Mode A: 1-change trigger + strict confirm (aggressive)
  - Mode B: Adaptive micro_deg_per_step based on ev_win
  - Mode C: Fast-start latch + confirm (recommended default)
- NEW: mdi_ev_win, latch/confirm state machine
- NEW: Presets: hand_sensitive, bench_tolerant

State Flow: STILL → NOISE → PRE_MOVEMENT → PRE_ROTATION → MOVEMENT
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple, Set
from collections import deque, Counter
from enum import Enum
import math

INF = float('inf')

class AwState(Enum):
    STILL = "STILL"
    NOISE = "NOISE"
    PRE_MOVEMENT = "PRE_MOVEMENT"
    PRE_ROTATION = "PRE_ROTATION"
    MOVEMENT = "MOVEMENT"

class AwReason(Enum):
    INIT = "INIT"
    STOP_GAP_TIMEOUT = "STOP_GAP_TIMEOUT"
    NO_DISP_ACTIVE = "NO_DISP_ACTIVE"
    HOLD_DECAY = "HOLD_DECAY"
    STILL_LOW_ACTIVITY = "STILL_LOW_ACTIVITY"
    NOISE_ACC_BELOW_THRESHOLD = "NOISE_ACC_BELOW_THRESHOLD"
    MDI_TRIGGER = "MDI_TRIGGER"
    MDI_TREMOR = "MDI_TREMOR"
    MDI_HOLD_TIMEOUT = "MDI_HOLD_TIMEOUT"
    MDI_TRIGGER_A = "MDI_TRIGGER_A"
    MDI_TRIGGER_A_DROPPED = "MDI_TRIGGER_A_DROPPED"
    MDI_TRIGGER_B = "MDI_TRIGGER_B"
    MDI_LATCH = "MDI_LATCH"
    MDI_LATCH_DROPPED = "MDI_LATCH_DROPPED"
    CANDIDATE_POOL = "CANDIDATE_POOL"
    CANDIDATE_DROPPED = "CANDIDATE_DROPPED"
    COMMIT_ANGLE = "COMMIT_ANGLE"
    COMMIT_REBOUND = "COMMIT_REBOUND"
    PRE_ROT_ORIGIN_SET = "PRE_ROT_ORIGIN_SET"
    MOVEMENT_DISP_CONFIRMED = "MOVEMENT_DISP_CONFIRMED"
    MOVEMENT_SPEED_CONFIRMED = "MOVEMENT_SPEED_CONFIRMED"
    MOVEMENT_LOCK_ACCELERATED = "MOVEMENT_LOCK_ACCELERATED"

class L1State(Enum):
    STILL = "STILL"
    FEELING = "FEELING"
    SCRAPE = "SCRAPE"
    DISPLACEMENT = "DISPLACEMENT"
    MOVING = "MOVING"

class L1Reason(Enum):
    STILL_GAP_TIMEOUT = "STILL_GAP_TIMEOUT"
    STILL_LOW_ACTIVITY = "STILL_LOW_ACTIVITY"
    FEELING_ACTIVITY_NO_DISP = "FEELING_ACTIVITY_NO_DISP"
    SCRAPE_HIGH_ACTIVITY = "SCRAPE_HIGH_ACTIVITY"
    DISP_ABOVE_D0 = "DISP_ABOVE_D0"
    MOVING_STABLE_DIR = "MOVING_STABLE_DIR"
    MOVING_LOCKED = "MOVING_LOCKED"
    HARD_RESET_GAP = "HARD_RESET_GAP"
    INIT = "INIT"

@dataclass
class L1Config:
    """Configuration for L1 PhysicalActivity + OriginTracker v0.4.5."""
    gap_ms: float = 500.0
    activity_threshold_low: float = 1.0
    activity_threshold_high: float = 5.0
    displacement_threshold: float = 0.005
    direction_conf_threshold: float = 0.5
    lock_states_for_moving: tuple = ("SOFT_LOCK", "LOCKED")
    cycles_per_rot: float = 12.0
    encoder_tau_s: float = 0.6
    hard_reset_s: float = 1.5
    activity_decay_rate: float = 5.0
    # MDI v0.4.5
    mdi_mode: str = "C"
    mdi_win_ms: float = 200.0
    mdi_valid_rate_min: float = 0.70
    mdi_tremor_max: float = 0.60
    mdi_conf_min: float = 0.35
    mdi_conf_tau_s: float = 0.30
    mdi_hold_s: float = 0.35
    mdi_micro_acc_max: float = 36.0
    mdi_flipflop_window_ms: float = 80.0
    # Mode A
    mdi_conf_min_A: float = 0.20
    mdi_trigger_changes_A: int = 1
    mdi_confirm_s_A: float = 0.25
    mdi_confirm_changes_A: int = 2
    # Mode B
    micro_deg_per_step_base: float = 10.0
    micro_deg_per_step_ev3: float = 15.0
    micro_deg_per_step_ev6: float = 12.0
    mdi_trigger_micro_deg: float = 20.0
    # Mode C
    mdi_latch_confirm_s: float = 0.25
    mdi_latch_drop_s: float = 0.35
    mdi_latch_min_changes: int = 1
    mdi_confirm_changes: int = 2
    mdi_confirm_micro_deg: float = 15.0
    mdi_confirm_conf: float = 0.35
    # Pool/Origin
    pool_win_ms: float = 250.0
    pool_changes_min: int = 2
    pool_unique_min: int = 2
    pool_valid_rate_min: float = 0.70
    origin_step_deg: float = 30.0
    origin_commit_horizon_s: float = 0.35
    origin_rebound_eps_deg: float = 10.0
    movement_confirm_deg: float = 60.0
    speed_confirm_deg_s: float = 30.0
    speed_ema_tau_s: float = 0.25
    stop_gap_s: float = 0.80
    noise_gap_s: float = 0.50
    movement_hold_s: float = 0.25
    activity_reset_a0: float = 0.20

@dataclass
class L1Snapshot:
    """Snapshot of L1 state + OriginTracker v0.4.5."""
    state: L1State
    reason: L1Reason
    theta_hat_rot: float = 0.0
    theta_hat_deg: float = 0.0
    delta_theta_deg_signed: float = 0.0
    activity_score: float = 0.0
    direction_effective: str = "UNDECIDED"
    direction_conf: float = 0.0
    lock_state: str = "UNLOCKED"
    encoder_conf: float = 0.0
    dt_s: float = 0.0
    t_last_cycle_s: Optional[float] = None
    t_last_event_s: Optional[float] = None
    total_cycles: float = 0.0
    delta_cycles: float = 0.0
    total_events: int = 0
    delta_events: int = 0
    ageE_s: float = INF
    ageC_s: float = INF
    l2_stale: bool = False
    to_pool_hist: Dict[str, int] = field(default_factory=dict)
    pool_changes_win: int = 0
    pool_unique_win: Set[int] = field(default_factory=set)
    pool_valid_rate_win: float = 0.0
    # MDI v0.4.5
    mdi_mode: str = "C"
    mdi_ev_win: int = 0
    mdi_micro_deg_per_step_used: float = 10.0
    mdi_micro_acc: float = 0.0
    mdi_disp_micro_deg: float = 0.0
    mdi_conf: float = 0.0
    mdi_conf_acc: float = 0.0
    mdi_conf_used: float = 0.0  # v0.4.5: conf_acc or conf fallback
    mdi_tremor_score: float = 0.0
    mdi_pool_changes: int = 0
    mdi_unique_pools: Set[int] = field(default_factory=set)
    mdi_valid_rate: float = 0.0
    micro_t0_s: Optional[float] = None
    micro_dir_hint: str = "UNDECIDED"
    mdi_latch_set: bool = False
    mdi_latch_t0_s: Optional[float] = None
    mdi_latch_age_s: Optional[float] = None
    mdi_changes_since_latch: int = 0
    mdi_confirmed: bool = False
    mdi_latch_reason: str = ""
    origin_candidate_set: bool = False
    origin_candidate_time_s: Optional[float] = None
    origin_commit_set: bool = False
    origin_time_s: Optional[float] = None
    origin_time0_s: Optional[float] = None
    origin_theta_deg: Optional[float] = None
    origin_conf: float = 0.0
    disp_acc_deg: float = 0.0
    disp_from_origin_deg: float = 0.0
    speed_deg_s: float = 0.0
    early_dir: str = "UNDECIDED"
    aw_state: AwState = AwState.STILL
    aw_reason: AwReason = AwReason.INIT

def wrap_deg_signed(x: float) -> float:
    return ((x + 180.0) % 360.0) - 180.0

class L1PhysicalActivity:
    """L1 PhysicalActivity Layer v1.1 (OriginTracker v0.4.5 + MDI modes)."""
    
    def __init__(self, config: L1Config = None):
        self.config = config or L1Config()
        self._state, self._reason = L1State.STILL, L1Reason.INIT
        self._theta_hat_rot = self._prev_theta_hat_rot = 0.0
        self._t_last_update = self._t_last_cycle_s = self._t_last_event_s = None
        self._prev_cycles_total = 0.0
        self._total_events = self._events_without_cycles = 0
        self._activity_score = self._encoder_conf = 0.0
        self._direction_effective, self._direction_conf, self._lock_state = "UNDECIDED", 0.0, "UNLOCKED"
        self._to_pool_hist = Counter()
        self._pool_window: deque = deque()
        self._mdi_window: deque = deque()
        self._mdi_micro_acc = self._mdi_tremor_score = self._mdi_conf_acc = 0.0
        self._mdi_conf_last_update_s = None
        self._mdi_last_pool_A = self._mdi_last_pool_B = self._mdi_last_sensor = None
        self._mdi_flipflop_buffer: deque = deque(maxlen=10)
        self._micro_t0_s = None
        self._micro_dir_hint = "UNDECIDED"
        self._mdi_pool_order: deque = deque(maxlen=6)
        # Mode C latch
        self._mdi_latch_set, self._mdi_latch_t0_s = False, None
        self._mdi_changes_since_latch, self._mdi_confirmed = 0, False
        self._mdi_latch_reason = ""
        # Mode A
        self._mdi_trigger_A_t0_s = None
        self._mdi_changes_since_trigger_A = 0
        # Origin
        self._origin_candidate_set, self._origin_candidate_time_s = False, None
        self._origin_candidate_conf = 0.0
        self._origin_commit_set, self._origin_time_s, self._origin_time0_s = False, None, None
        self._origin_theta_hat_rot, self._origin_conf = None, 0.0
        self._disp_acc_deg = self._disp_from_origin_deg = self._speed_deg_s = 0.0
        self._prev_disp_from_origin_deg = 0.0
        self._early_dir = "UNDECIDED"
        self._commit_horizon_start_s, self._commit_horizon_max_acc = None, 0.0
        self._aw_state, self._aw_reason = AwState.STILL, AwReason.INIT
    
    @property
    def state(self) -> L1State: return self._state
    @property
    def aw_state(self) -> AwState: return self._aw_state
    
    def record_pool(self, to_pool, sensor: int, t_s: float = None) -> None:
        cfg = self.config
        now_s = t_s or (self._t_last_update or 0.0)
        key = "None" if to_pool is None else (str(to_pool) if to_pool in (0,1,2,3) else "other")
        pool_val = int(to_pool) if to_pool in (0,1,2,3) else None
        self._to_pool_hist[key] += 1
        self._pool_window.append((now_s, sensor, pool_val))
        while self._pool_window and self._pool_window[0][0] < now_s - cfg.pool_win_ms/1000: self._pool_window.popleft()
        self._mdi_window.append((now_s, sensor, pool_val))
        while self._mdi_window and self._mdi_window[0][0] < now_s - cfg.mdi_win_ms/1000: self._mdi_window.popleft()
        if pool_val in (0,1,2): self._process_mdi_step(now_s, sensor, pool_val)
    
    def _process_mdi_step(self, t_s: float, sensor: int, pool_val: int) -> None:
        cfg = self.config
        prev = self._mdi_last_pool_A if sensor == 0 else self._mdi_last_pool_B
        if sensor == 0: self._mdi_last_pool_A = pool_val
        else: self._mdi_last_pool_B = pool_val
        if prev is not None and prev != pool_val:
            step = 1.0
            self._mdi_pool_order.append((pool_val, sensor))
            if self._mdi_latch_set: self._mdi_changes_since_latch += 1
            if self._mdi_trigger_A_t0_s is not None: self._mdi_changes_since_trigger_A += 1
            self._mdi_flipflop_buffer.append((t_s, sensor, pool_val))
            cutoff = t_s - cfg.mdi_flipflop_window_ms/1000
            recent = [p for tt,_,p in self._mdi_flipflop_buffer if tt >= cutoff]
            if len(recent) >= 3 and recent[-3] == recent[-1] != recent[-2]:
                step, self._mdi_tremor_score = -0.5, min(1.0, self._mdi_tremor_score + 0.15)
            self._mdi_micro_acc = max(0, min(cfg.mdi_micro_acc_max, self._mdi_micro_acc + step))
            if self._micro_t0_s is None and self._mdi_micro_acc >= 1: self._micro_t0_s = t_s
        self._mdi_last_sensor = sensor
        self._mdi_tremor_score = max(0, self._mdi_tremor_score - 0.02)
    
    def _compute_mdi_stats(self, now_s: float):
        cfg = self.config
        cutoff = now_s - cfg.mdi_win_ms/1000
        ev_win, changes, valid_count, switches = 0, 0, 0, 0
        unique: Set[int] = set()
        pA = pB = ps = None
        for t, s, p in self._mdi_window:
            if t < cutoff: continue
            ev_win += 1
            if p in (0,1,2):
                valid_count += 1
                unique.add(p)
                if s == 0:
                    if pA is not None and pA != p: changes += 1
                    pA = p
                else:
                    if pB is not None and pB != p: changes += 1
                    pB = p
            if ps is not None and ps != s: switches += 1
            ps = s
        vr = valid_count/ev_win if ev_win else 0
        ar = switches/max(1, ev_win-1) if ev_win > 1 else 0
        return ev_win, changes, unique, vr, ar, self._mdi_tremor_score
    
    def _compute_mdi_conf(self, chg, uniq, vr, ar, trem):
        return max(0, min(1, 0.3*min(1,chg/4) + 0.2*len(uniq&{0,1,2})/3 + 0.2*vr + 0.2*min(1,ar*2) - 0.3*trem))
    
    def _update_mdi_conf_acc(self, conf, now_s):
        cfg = self.config
        if self._mdi_conf_last_update_s is None: self._mdi_conf_acc = conf
        else:
            dt = now_s - self._mdi_conf_last_update_s
            a = 1 - math.exp(-dt/cfg.mdi_conf_tau_s) if cfg.mdi_conf_tau_s > 0 else 1
            self._mdi_conf_acc = (1-a)*self._mdi_conf_acc + a*conf
        self._mdi_conf_last_update_s = now_s
        return self._mdi_conf_acc
    
    def _get_step_size(self, ev_win):
        cfg = self.config
        if cfg.mdi_mode == "B":
            if ev_win <= 3: return cfg.micro_deg_per_step_ev3
            if ev_win <= 6: return cfg.micro_deg_per_step_ev6
        return cfg.micro_deg_per_step_base
    
    def _infer_dir(self):
        if len(self._mdi_pool_order) < 3: return "UNDECIDED"
        pools = [p for p,_ in list(self._mdi_pool_order)[-6:]]
        ns = sum(1 for i in range(len(pools)-1) if pools[i]==1 and pools[i+1]==2)
        sn = sum(1 for i in range(len(pools)-1) if pools[i]==2 and pools[i+1]==1)
        if ns > sn+1: return "CW"
        if sn > ns+1: return "CCW"
        return "UNDECIDED"
    
    def _apply_mode_A(self, now_s, chg, vr, conf_used, trem, micro_deg):
        cfg = self.config
        entry = chg >= cfg.mdi_trigger_changes_A and vr >= cfg.mdi_valid_rate_min and conf_used >= cfg.mdi_conf_min_A and trem <= cfg.mdi_tremor_max
        if entry and self._mdi_trigger_A_t0_s is None:
            self._mdi_trigger_A_t0_s, self._mdi_changes_since_trigger_A = now_s, 0
            if self._micro_t0_s is None: self._micro_t0_s = now_s
            return True, AwReason.MDI_TRIGGER_A
        if self._mdi_trigger_A_t0_s:
            age = now_s - self._mdi_trigger_A_t0_s
            if self._mdi_changes_since_trigger_A >= cfg.mdi_confirm_changes_A or micro_deg >= cfg.mdi_trigger_micro_deg:
                return True, AwReason.MDI_TRIGGER_A
            if age <= cfg.mdi_confirm_s_A: return True, AwReason.MDI_TRIGGER_A
            self._mdi_trigger_A_t0_s, self._mdi_changes_since_trigger_A = None, 0
            return False, AwReason.MDI_TRIGGER_A_DROPPED
        return False, AwReason.NOISE_ACC_BELOW_THRESHOLD
    
    def _apply_mode_B(self, now_s, chg, vr, conf_used, trem, micro_deg):
        cfg = self.config
        if micro_deg >= cfg.mdi_trigger_micro_deg and conf_used >= cfg.mdi_conf_min and trem <= cfg.mdi_tremor_max:
            if self._micro_t0_s is None: self._micro_t0_s = now_s
            return True, AwReason.MDI_TRIGGER_B
        return False, AwReason.NOISE_ACC_BELOW_THRESHOLD
    
    def _apply_mode_C(self, now_s, chg, vr, conf_used, trem, micro_deg):
        cfg = self.config
        if not self._mdi_latch_set:
            if chg >= cfg.mdi_latch_min_changes and vr >= cfg.mdi_valid_rate_min and trem <= cfg.mdi_tremor_max:
                self._mdi_latch_set, self._mdi_latch_t0_s = True, now_s
                self._mdi_changes_since_latch, self._mdi_confirmed = 0, False
                self._mdi_latch_reason = "MDI_LATCH"
                if self._micro_t0_s is None: self._micro_t0_s = now_s
                return True, AwReason.MDI_LATCH
        if self._mdi_latch_set:
            age = now_s - self._mdi_latch_t0_s if self._mdi_latch_t0_s else 0
            if not self._mdi_confirmed and age <= cfg.mdi_latch_confirm_s:
                if self._mdi_changes_since_latch >= cfg.mdi_confirm_changes or micro_deg >= cfg.mdi_confirm_micro_deg or conf_used >= cfg.mdi_confirm_conf:
                    self._mdi_confirmed, self._mdi_latch_reason = True, "MDI_TRIGGER"
                    return True, AwReason.MDI_TRIGGER
            if self._mdi_confirmed: return True, AwReason.MDI_TRIGGER
            if age <= cfg.mdi_latch_confirm_s: return True, AwReason.MDI_LATCH
            if age > cfg.mdi_latch_drop_s and not self._mdi_confirmed:
                self._mdi_latch_set, self._mdi_latch_t0_s = False, None
                self._mdi_changes_since_latch = 0
                self._mdi_latch_reason = "MDI_LATCH_DROPPED"
                self._mdi_micro_acc *= 0.5
                return False, AwReason.MDI_LATCH_DROPPED
            return True, AwReason.MDI_LATCH
        return False, AwReason.NOISE_ACC_BELOW_THRESHOLD
    
    def _apply_mdi_mode(self, now_s, ev_win, chg, uniq, vr, conf, conf_acc, trem, micro_deg):
        mode = self.config.mdi_mode.upper()
        conf_used = conf_acc if conf_acc > 0 else conf
        if mode == "A": return self._apply_mode_A(now_s, chg, vr, conf_used, trem, micro_deg)
        if mode == "B": return self._apply_mode_B(now_s, chg, vr, conf_used, trem, micro_deg)
        return self._apply_mode_C(now_s, chg, vr, conf_used, trem, micro_deg)
    
    def _compute_pool_stats(self, now_s):
        cfg = self.config
        cutoff = now_s - cfg.pool_win_ms/1000
        chg, valid = 0, 0
        unique: Set[int] = set()
        pA = pB = None
        total = 0
        for t, s, p in self._pool_window:
            if t < cutoff: continue
            total += 1
            if p in (0,1,2):
                valid += 1
                unique.add(p)
                if s == 0:
                    if pA is not None and pA != p: chg += 1
                    pA = p
                else:
                    if pB is not None and pB != p: chg += 1
                    pB = p
        vr = valid/total if total else 0
        return chg, unique, vr
    
    def _reset_origin(self, reason: str, keep_tactile=False, reset_mdi=True):
        cfg = self.config
        if reset_mdi:
            self._mdi_micro_acc = self._mdi_tremor_score = self._mdi_conf_acc = 0
            self._mdi_conf_last_update_s = self._micro_t0_s = None
            self._micro_dir_hint = "UNDECIDED"
            self._mdi_pool_order.clear()
            self._mdi_last_pool_A = self._mdi_last_pool_B = self._mdi_last_sensor = None
            self._mdi_flipflop_buffer.clear()
            self._mdi_latch_set, self._mdi_latch_t0_s = False, None
            self._mdi_changes_since_latch, self._mdi_confirmed = 0, False
            self._mdi_latch_reason = ""
            self._mdi_trigger_A_t0_s, self._mdi_changes_since_trigger_A = None, 0
        self._origin_candidate_set, self._origin_candidate_time_s = False, None
        self._origin_candidate_conf = 0
        self._origin_commit_set, self._origin_time_s, self._origin_time0_s = False, None, None
        self._origin_theta_hat_rot, self._origin_conf = None, 0
        self._disp_acc_deg = self._disp_from_origin_deg = self._speed_deg_s = 0
        self._prev_disp_from_origin_deg = 0
        self._early_dir = "UNDECIDED"
        self._commit_horizon_start_s, self._commit_horizon_max_acc = None, 0
        self._aw_state = AwState.NOISE if keep_tactile and self._activity_score >= cfg.activity_threshold_low else AwState.STILL
        rmap = {"STOP_GAP_TIMEOUT": AwReason.STOP_GAP_TIMEOUT, "NO_DISP_ACTIVE": AwReason.NO_DISP_ACTIVE,
                "MDI_TREMOR": AwReason.MDI_TREMOR, "MDI_HOLD_TIMEOUT": AwReason.MDI_HOLD_TIMEOUT,
                "MDI_LATCH_DROPPED": AwReason.MDI_LATCH_DROPPED, "CANDIDATE_DROPPED": AwReason.CANDIDATE_DROPPED}
        self._aw_reason = rmap.get(reason, AwReason.INIT)
    
    def update(self, wall_time: float, cycles_physical_total: float, events_this_batch: int = 0,
               direction_conf: float = None, lock_state: str = None, direction_effective: str = None, **kw) -> L1Snapshot:
        cfg = self.config
        now_s = wall_time
        dt_s = (now_s - self._t_last_update) if self._t_last_update else 0
        self._t_last_update = now_s
        if dt_s > cfg.hard_reset_s: self._hard_reset(); dt_s = 0
        
        delta_cycles = cycles_physical_total - self._prev_cycles_total
        self._prev_cycles_total = cycles_physical_total
        self._total_events += events_this_batch
        self._prev_theta_hat_rot = self._theta_hat_rot
        self._theta_hat_rot = cycles_physical_total / cfg.cycles_per_rot
        dtheta = wrap_deg_signed((self._theta_hat_rot - self._prev_theta_hat_rot) * 360)
        theta_deg = (self._theta_hat_rot * 360) % 360
        
        if delta_cycles > 0: self._t_last_cycle_s, self._events_without_cycles = now_s, 0
        if events_this_batch > 0:
            self._t_last_event_s = now_s
            if delta_cycles == 0: self._events_without_cycles += events_this_batch
        
        ageE = INF if self._t_last_event_s is None else now_s - self._t_last_event_s
        ageC = INF if self._t_last_cycle_s is None else now_s - self._t_last_cycle_s
        l2_stale = ageC >= cfg.stop_gap_s
        
        if direction_conf is not None: self._direction_conf = direction_conf
        if lock_state is not None: self._lock_state = lock_state
        if direction_effective is not None: self._direction_effective = direction_effective
        
        if dt_s > 0: self._activity_score *= math.exp(-dt_s * cfg.activity_decay_rate)
        self._activity_score += events_this_batch
        if dt_s > 0: self._encoder_conf *= math.exp(-dt_s / cfg.encoder_tau_s)
        if delta_cycles > 0: self._encoder_conf = min(1, self._encoder_conf + 0.15)
        elif events_this_batch > 0: self._encoder_conf = min(1, self._encoder_conf + 0.05)
        self._encoder_conf = max(0, min(1, self._encoder_conf))
        
        gap_ms_C, gap_ms_E = ageC*1000 if ageC != INF else INF, ageE*1000 if ageE != INF else INF
        self._state, self._reason = self._compute_l1_state(self._activity_score, abs(dtheta/360), gap_ms_C, gap_ms_E)
        
        pool_chg, pool_uniq, pool_vr = self._compute_pool_stats(now_s)
        ev_win, mdi_chg, mdi_uniq, mdi_vr, mdi_ar, mdi_trem = self._compute_mdi_stats(now_s)
        mdi_conf = self._compute_mdi_conf(mdi_chg, mdi_uniq, mdi_vr, mdi_ar, mdi_trem)
        mdi_conf_acc = self._update_mdi_conf_acc(mdi_conf, now_s)
        step_size = self._get_step_size(ev_win)
        mdi_deg = self._mdi_micro_acc * step_size
        self._micro_dir_hint = self._infer_dir()
        
        # v0.4.5: Evaluate MDI FIRST to check if we should skip gap reset
        mdi_triggered, mdi_reason = False, AwReason.NOISE_ACC_BELOW_THRESHOLD
        if mdi_trem <= cfg.mdi_tremor_max:  # only if not tremoring
            mdi_triggered, mdi_reason = self._apply_mdi_mode(now_s, ev_win, mdi_chg, mdi_uniq, mdi_vr, mdi_conf, mdi_conf_acc, mdi_trem, mdi_deg)
        
        # MDI is "active" if latched or triggered
        mdi_active = mdi_triggered or self._mdi_latch_set or self._aw_state == AwState.PRE_MOVEMENT
        
        gap_handled = False
        if (ageC >= cfg.stop_gap_s or ageE >= cfg.stop_gap_s) and self._activity_score < cfg.activity_reset_a0:
            # Hard gap: always reset
            self._reset_origin("STOP_GAP_TIMEOUT", False, True); gap_handled = True
        elif ageC >= cfg.noise_gap_s and self._activity_score >= cfg.activity_reset_a0 and not mdi_active:
            # Soft gap: only reset if MDI is NOT active (v0.4.5 fix)
            self._reset_origin("NO_DISP_ACTIVE", True, False); gap_handled = True
        elif self._origin_commit_set and cfg.movement_hold_s < ageC < cfg.stop_gap_s:
            if self._aw_state == AwState.MOVEMENT:
                self._aw_state, self._aw_reason = AwState.PRE_ROTATION, AwReason.HOLD_DECAY
            self._speed_deg_s *= 0.9
        
        # MDI state transitions (after gap check)
        if not gap_handled:
            if mdi_trem > cfg.mdi_tremor_max and self._aw_state == AwState.PRE_MOVEMENT:
                self._reset_origin("MDI_TREMOR", True, True); gap_handled = True
            elif self._aw_state == AwState.PRE_MOVEMENT and not self._origin_candidate_set:
                if ageE > cfg.mdi_hold_s and self._activity_score < cfg.activity_threshold_low:
                    self._reset_origin("MDI_HOLD_TIMEOUT", False, True); gap_handled = True
            if not gap_handled:
                # Apply MDI state change
                if mdi_triggered and self._aw_state in (AwState.STILL, AwState.NOISE):
                    self._aw_state, self._aw_reason = AwState.PRE_MOVEMENT, mdi_reason
                elif mdi_reason in (AwReason.MDI_LATCH_DROPPED, AwReason.MDI_TRIGGER_A_DROPPED):
                    if self._aw_state == AwState.PRE_MOVEMENT:
                        self._aw_state = AwState.NOISE if self._activity_score >= cfg.activity_threshold_low else AwState.STILL
                        self._aw_reason = mdi_reason
        
        if not gap_handled:
            self._disp_acc_deg += dtheta
            if not self._origin_commit_set:
                valid_pools = pool_uniq & {0,1,2}
                strong = pool_chg >= cfg.pool_changes_min and len(valid_pools) >= cfg.pool_unique_min and pool_vr >= cfg.pool_valid_rate_min
                if strong and not self._origin_candidate_set:
                    self._origin_candidate_set, self._origin_candidate_time_s = True, now_s
                    self._origin_candidate_conf = min(1, 0.3 + 0.2*(pool_chg/5) + 0.2*(len(valid_pools)/3) + 0.3*pool_vr)
                    if self._origin_time0_s is None: self._origin_time0_s = self._micro_t0_s or now_s
                    if self._aw_state in (AwState.STILL, AwState.NOISE, AwState.PRE_MOVEMENT):
                        self._aw_state, self._aw_reason = AwState.PRE_ROTATION, AwReason.CANDIDATE_POOL
                elif self._origin_candidate_set and not strong:
                    if pool_chg == 0 and self._activity_score < cfg.activity_threshold_low:
                        self._reset_origin("CANDIDATE_DROPPED", False, True); gap_handled = True
            
            if not gap_handled and not self._origin_commit_set:
                abs_acc = abs(self._disp_acc_deg)
                if abs_acc >= cfg.origin_step_deg and self._commit_horizon_start_s is None:
                    self._commit_horizon_start_s, self._commit_horizon_max_acc = now_s, abs_acc
                if self._commit_horizon_start_s:
                    h_age = now_s - self._commit_horizon_start_s
                    self._commit_horizon_max_acc = max(self._commit_horizon_max_acc, abs_acc)
                    if abs_acc < cfg.origin_rebound_eps_deg:
                        self._commit_horizon_start_s, self._commit_horizon_max_acc = None, 0
                        if self._origin_candidate_set: self._aw_reason = AwReason.COMMIT_REBOUND
                    elif h_age >= cfg.origin_commit_horizon_s:
                        self._origin_commit_set, self._origin_time_s = True, now_s
                        if self._origin_time0_s is None: self._origin_time0_s = self._micro_t0_s or self._origin_candidate_time_s or now_s
                        self._origin_theta_hat_rot = self._theta_hat_rot - self._disp_acc_deg/360
                        self._origin_conf = 0.6
                        self._aw_state, self._aw_reason = AwState.PRE_ROTATION, AwReason.COMMIT_ANGLE
                        self._commit_horizon_start_s, self._commit_horizon_max_acc = None, 0
            
            if self._origin_commit_set and self._origin_theta_hat_rot is not None:
                self._disp_from_origin_deg = wrap_deg_signed((self._theta_hat_rot - self._origin_theta_hat_rot)*360)
            if dt_s > 0:
                delta_d = wrap_deg_signed(self._disp_from_origin_deg - self._prev_disp_from_origin_deg)
                alpha = 1 - math.exp(-dt_s/cfg.speed_ema_tau_s)
                self._speed_deg_s = (1-alpha)*self._speed_deg_s + alpha*abs(delta_d)/dt_s
            self._prev_disp_from_origin_deg = self._disp_from_origin_deg
            if abs(self._disp_from_origin_deg) >= 15: self._early_dir = "CW" if self._disp_from_origin_deg > 0 else "CCW"
            elif abs(self._disp_acc_deg) >= 15: self._early_dir = "CW" if self._disp_acc_deg > 0 else "CCW"
            elif self._micro_dir_hint != "UNDECIDED": self._early_dir = self._micro_dir_hint
            
            if not gap_handled: self._aw_state, self._aw_reason = self._compute_aw(mdi_triggered, mdi_reason)
            if self._origin_commit_set:
                if abs(self._disp_from_origin_deg) > cfg.movement_confirm_deg: self._origin_conf = min(1, self._origin_conf + 0.1*dt_s)
                elif self._speed_deg_s > cfg.speed_confirm_deg_s: self._origin_conf = min(1, self._origin_conf + 0.05*dt_s)
        
        latch_age = (now_s - self._mdi_latch_t0_s) if self._mdi_latch_set and self._mdi_latch_t0_s else None
        mdi_conf_used = mdi_conf_acc if mdi_conf_acc > 0 else mdi_conf  # v0.4.5: conf_used
        return L1Snapshot(state=self._state, reason=self._reason, theta_hat_rot=self._theta_hat_rot, theta_hat_deg=theta_deg,
            delta_theta_deg_signed=dtheta, activity_score=self._activity_score, direction_effective=self._direction_effective,
            direction_conf=self._direction_conf, lock_state=self._lock_state, encoder_conf=self._encoder_conf, dt_s=dt_s,
            t_last_cycle_s=self._t_last_cycle_s, t_last_event_s=self._t_last_event_s, total_cycles=cycles_physical_total,
            delta_cycles=delta_cycles, total_events=self._total_events, delta_events=events_this_batch, ageE_s=ageE, ageC_s=ageC,
            l2_stale=l2_stale, to_pool_hist=dict(self._to_pool_hist), pool_changes_win=pool_chg, pool_unique_win=pool_uniq,
            pool_valid_rate_win=pool_vr, mdi_mode=cfg.mdi_mode, mdi_ev_win=ev_win, mdi_micro_deg_per_step_used=step_size,
            mdi_micro_acc=self._mdi_micro_acc, mdi_disp_micro_deg=mdi_deg, mdi_conf=mdi_conf, mdi_conf_acc=mdi_conf_acc,
            mdi_conf_used=mdi_conf_used,  # v0.4.5: CRITICAL wiring
            mdi_tremor_score=mdi_trem, mdi_pool_changes=mdi_chg, mdi_unique_pools=mdi_uniq, mdi_valid_rate=mdi_vr,
            micro_t0_s=self._micro_t0_s, micro_dir_hint=self._micro_dir_hint, mdi_latch_set=self._mdi_latch_set,
            mdi_latch_t0_s=self._mdi_latch_t0_s, mdi_latch_age_s=latch_age, mdi_changes_since_latch=self._mdi_changes_since_latch,
            mdi_confirmed=self._mdi_confirmed, mdi_latch_reason=self._mdi_latch_reason,
            origin_candidate_set=self._origin_candidate_set, origin_candidate_time_s=self._origin_candidate_time_s,
            origin_commit_set=self._origin_commit_set, origin_time_s=self._origin_time_s, origin_time0_s=self._origin_time0_s,
            origin_theta_deg=(self._origin_theta_hat_rot*360)%360 if self._origin_theta_hat_rot else None,
            origin_conf=self._origin_conf, disp_acc_deg=self._disp_acc_deg, disp_from_origin_deg=self._disp_from_origin_deg,
            speed_deg_s=self._speed_deg_s, early_dir=self._early_dir, aw_state=self._aw_state, aw_reason=self._aw_reason)
    
    def _compute_aw(self, mdi_trig, mdi_r):
        cfg = self.config
        if self._origin_commit_set:
            if abs(self._disp_from_origin_deg) >= cfg.movement_confirm_deg: return AwState.MOVEMENT, AwReason.MOVEMENT_DISP_CONFIRMED
            if self._speed_deg_s >= cfg.speed_confirm_deg_s: return AwState.MOVEMENT, AwReason.MOVEMENT_SPEED_CONFIRMED
            if self._lock_state in cfg.lock_states_for_moving: return AwState.MOVEMENT, AwReason.MOVEMENT_LOCK_ACCELERATED
            return AwState.PRE_ROTATION, AwReason.PRE_ROT_ORIGIN_SET
        if self._origin_candidate_set: return AwState.PRE_ROTATION, AwReason.CANDIDATE_POOL
        if mdi_trig: return AwState.PRE_MOVEMENT, mdi_r
        if self._activity_score >= cfg.activity_threshold_low: return AwState.NOISE, AwReason.NOISE_ACC_BELOW_THRESHOLD
        return AwState.STILL, AwReason.STILL_LOW_ACTIVITY
    
    def _compute_l1_state(self, act, disp, gap_C, gap_E):
        cfg = self.config
        if gap_C >= cfg.gap_ms and gap_E >= cfg.gap_ms: return L1State.STILL, L1Reason.STILL_GAP_TIMEOUT
        if act < cfg.activity_threshold_low and disp < cfg.displacement_threshold: return L1State.STILL, L1Reason.STILL_LOW_ACTIVITY
        if disp >= cfg.displacement_threshold:
            if self._lock_state in cfg.lock_states_for_moving: return L1State.MOVING, L1Reason.MOVING_LOCKED
            if self._direction_conf >= cfg.direction_conf_threshold: return L1State.MOVING, L1Reason.MOVING_STABLE_DIR
            return L1State.DISPLACEMENT, L1Reason.DISP_ABOVE_D0
        if act >= cfg.activity_threshold_high: return L1State.SCRAPE, L1Reason.SCRAPE_HIGH_ACTIVITY
        if act >= cfg.activity_threshold_low: return L1State.FEELING, L1Reason.FEELING_ACTIVITY_NO_DISP
        return L1State.STILL, L1Reason.STILL_LOW_ACTIVITY
    
    def _hard_reset(self):
        self._state, self._encoder_conf, self._activity_score, self._events_without_cycles = L1State.STILL, 0, 0, 0
        self._reset_origin("HARD_RESET", False, True)
    
    def reset(self): self.__init__(self.config)

# Presets
L1_CONFIG_DEFAULT = L1Config()
L1_CONFIG_HAND_SENSITIVE = L1Config(origin_step_deg=15, mdi_mode="C", mdi_confirm_micro_deg=15, mdi_conf_min=0.30, movement_confirm_deg=45)
L1_CONFIG_BENCH_TOLERANT = L1Config(origin_step_deg=30, mdi_mode="B", mdi_trigger_micro_deg=20, mdi_win_ms=250, stop_gap_s=1.0)
L1_CONFIG_AGGRESSIVE = L1Config(origin_step_deg=15, mdi_mode="A", mdi_conf_min_A=0.15, mdi_confirm_s_A=0.30)
L1_CONFIG_HUMAN = L1_CONFIG_DEFAULT
L1_CONFIG_BENCH = L1_CONFIG_BENCH_TOLERANT
L1_CONFIG_SENSITIVE = L1_CONFIG_HAND_SENSITIVE
