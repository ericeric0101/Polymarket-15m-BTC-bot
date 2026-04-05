#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parents[0]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pure_signal_probe import PureSignalProbe, _extract_question, _run_id, _safe_float, _utc_now  # noqa: E402
from execution.maker_engine import MakerEngine  # noqa: E402


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _norm_tanh(value: Optional[float], scale: float) -> float:
    if value is None or scale <= 0:
        return 0.0
    return math.tanh(value / scale)


@dataclass
class ShadowProbeConfig:
    db_path: str
    interval_sec: float
    duration_sec: float
    verbose: bool
    verbose_every_sec: float
    min_edge: Decimal
    min_prob_band: Decimal
    max_prob_band: Decimal
    min_entry_sec: float
    reduce_only_sec: float
    force_flat_sec: float
    sigma_default: Decimal
    sigma_floor: Decimal
    sigma_ceiling: Decimal
    sigma_min_points: int
    sigma_window_points: int
    paper_trade: bool
    paper_persistence_sec: float
    paper_settle_grace_sec: float
    paper_entry_qty: float
    paper_exit_policy: str
    paper_exit_min_hold_sec: float
    paper_take_profit: float
    paper_stop_loss: float
    paper_flip_exit_min_score: float
    strike_z_weight: float
    ret_10_weight: float
    ret_30_weight: float
    persistence_weight: float
    strike_z_scale: float
    ret_10_bps_scale: float
    ret_30_bps_scale: float
    shadow_score_min_abs: float
    breakout_window_sec: float
    ret_10_lookback_sec: float
    ret_30_lookback_sec: float


class ShadowFeatureProbe(PureSignalProbe):
    def __init__(self, cfg: ShadowProbeConfig) -> None:
        super().__init__(cfg)
        self.cfg = cfg
        self.run_id = _run_id("shadow_probe")
        self.last_candidate_signature = None

    def _paper_mark_price(self, snapshot: Dict[str, Any], side: str) -> Optional[float]:
        if side == "BUY_UP":
            value = snapshot.get("bid_up")
        elif side == "BUY_DOWN":
            value = snapshot.get("bid_down")
        else:
            return None
        try:
            if value is None:
                return None
            value_f = float(value)
            return value_f if value_f > 0 else None
        except Exception:
            return None

    def _maybe_close_paper_trade(self, slug: str, snapshot: Dict[str, Any]) -> bool:
        if self.cfg.paper_exit_policy != "simple":
            return False
        position = self.paper_positions_by_slug.get(slug)
        if not position:
            return False

        side = str(position.get("side") or "")
        entry_price = float(position.get("entry_price") or 0.0)
        if entry_price <= 0 or side not in {"BUY_UP", "BUY_DOWN"}:
            return False

        mark_price = self._paper_mark_price(snapshot, side)
        if mark_price is None:
            return False

        now_ts = time.time()
        try:
            entry_ts = datetime.fromisoformat(str(position.get("entry_ts"))).timestamp()
        except Exception:
            entry_ts = now_ts
        hold_sec = max(0.0, now_ts - entry_ts)
        if hold_sec < self.cfg.paper_exit_min_hold_sec:
            return False

        if mark_price >= (entry_price + self.cfg.paper_take_profit):
            self._close_paper_trade(
                slug=slug,
                exit_price=mark_price,
                close_kind="policy_exit",
                exit_reason="take_profit",
                snapshot=snapshot,
            )
            return True

        if mark_price <= max(0.0, entry_price - self.cfg.paper_stop_loss):
            self._close_paper_trade(
                slug=slug,
                exit_price=mark_price,
                close_kind="policy_exit",
                exit_reason="stop_loss",
                snapshot=snapshot,
            )
            return True

        candidate_side = str(snapshot.get("candidate_side") or "")
        shadow_score = float(snapshot.get("shadow_score") or 0.0)
        opposite_side = "BUY_DOWN" if side == "BUY_UP" else "BUY_UP"
        matured = self._candidate_matured_payload(slug)
        if (
            matured
            and candidate_side == opposite_side
            and str(matured.get("candidate_side") or "") == opposite_side
            and abs(shadow_score) >= self.cfg.paper_flip_exit_min_score
        ):
            self._close_paper_trade(
                slug=slug,
                exit_price=mark_price,
                close_kind="policy_exit",
                exit_reason="flip_confirmed",
                snapshot=snapshot,
            )
            return True

        return False

    def _close_paper_trade(
        self,
        *,
        slug: str,
        exit_price: float,
        close_kind: str,
        exit_reason: str,
        snapshot: Dict[str, Any],
    ) -> None:
        position = self.paper_positions_by_slug.get(slug)
        if not position:
            return
        side = str(position.get("side") or "")
        entry_price = float(position.get("entry_price") or 0.0)
        pnl = exit_price - entry_price
        close_payload = {
            **position,
            "exit_ts": _utc_now().isoformat(),
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "close_kind": close_kind,
            "exit_reason": exit_reason,
            "close_spot": _safe_float(Decimal(str(snapshot.get("spot")))) if snapshot.get("spot") is not None else None,
            "close_strike": snapshot.get("strike"),
            "close_shadow_score": snapshot.get("shadow_score"),
            "close_candidate_side": snapshot.get("candidate_side"),
        }
        self.db.log_strategy_event(self.run_id, "SHADOW_PAPER_TRADE_EXIT", close_payload)
        logger.info(
            f"SHADOW paper exit slug={slug} side={side} reason={exit_reason} "
            f"entry={entry_price:.4f} exit={exit_price:.4f} pnl={pnl:+.4f}"
        )
        self.paper_settled_slugs.add(slug)
        self.paper_positions_by_slug.pop(slug, None)
        self.candidate_streaks.pop(slug, None)

    def _spot_at_or_before(self, cutoff_ts: float) -> Optional[Decimal]:
        for ts, price in reversed(self.spot_history):
            if ts <= cutoff_ts:
                return price
        return None

    def _return_bps(self, spot: Decimal, lookback_sec: float) -> Optional[float]:
        if lookback_sec <= 0:
            return None
        hist_spot = self._spot_at_or_before(time.time() - lookback_sec)
        if hist_spot is None or hist_spot <= 0:
            return None
        try:
            return (float(spot / hist_spot) - 1.0) * 10000.0
        except Exception:
            return None

    def _breakout_persistence(self, spot: Decimal, strike: Decimal, window_sec: float) -> Optional[float]:
        if window_sec <= 0:
            return None
        cutoff_ts = time.time() - window_sec
        window = [(ts, px) for ts, px in self.spot_history if ts >= cutoff_ts]
        if not window:
            return None
        current_sign = 1 if spot > strike else -1
        aligned = 0
        for _, px in window:
            px_sign = 1 if px > strike else -1
            if px_sign == current_sign:
                aligned += 1
        return aligned / len(window)

    def _strike_z(self, spot: Decimal, strike: Decimal, sigma: Decimal, time_left_sec: float) -> float:
        if strike <= 0 or sigma <= 0 or time_left_sec <= 0:
            return 0.0
        t_years = time_left_sec / (365.0 * 24.0 * 3600.0)
        if t_years <= 0:
            return 0.0
        sigma_f = float(sigma)
        denom = sigma_f * math.sqrt(t_years)
        if denom <= 0:
            return 0.0
        try:
            numer = math.log(float(spot / strike)) - 0.5 * (sigma_f ** 2) * t_years
            return numer / denom
        except Exception:
            return 0.0

    def _shadow_signal(
        self,
        *,
        spot: Decimal,
        strike: Optional[Decimal],
        sigma: Decimal,
        time_left_sec: float,
    ) -> Dict[str, Optional[float]]:
        if strike is None or strike <= 0:
            return {
                "spot_minus_strike": None,
                "spot_minus_strike_bps": None,
                "ret_10_bps": None,
                "ret_30_bps": None,
                "breakout_persistence_60s": None,
                "strike_z": None,
                "shadow_score": 0.0,
                "shadow_prob_up": 0.5,
                "shadow_prob_down": 0.5,
            }

        spot_minus_strike = float(spot - strike)
        spot_minus_strike_bps = (float(spot / strike) - 1.0) * 10000.0
        ret_10_bps = self._return_bps(spot=spot, lookback_sec=self.cfg.ret_10_lookback_sec)
        ret_30_bps = self._return_bps(spot=spot, lookback_sec=self.cfg.ret_30_lookback_sec)
        breakout_persistence = self._breakout_persistence(
            spot=spot,
            strike=strike,
            window_sec=self.cfg.breakout_window_sec,
        )
        strike_z = self._strike_z(spot=spot, strike=strike, sigma=sigma, time_left_sec=time_left_sec)

        strike_component = _norm_tanh(strike_z, self.cfg.strike_z_scale)
        ret10_component = _norm_tanh(ret_10_bps, self.cfg.ret_10_bps_scale)
        ret30_component = _norm_tanh(ret_30_bps, self.cfg.ret_30_bps_scale)
        persistence_component = 0.0 if breakout_persistence is None else ((2.0 * breakout_persistence) - 1.0)

        shadow_score = (
            (self.cfg.strike_z_weight * strike_component)
            + (self.cfg.ret_10_weight * ret10_component)
            + (self.cfg.ret_30_weight * ret30_component)
            + (self.cfg.persistence_weight * persistence_component)
        )
        shadow_score = _clamp(shadow_score, -1.0, 1.0)
        shadow_prob_up = _clamp(0.5 + (0.49 * shadow_score), 0.01, 0.99)
        shadow_prob_down = 1.0 - shadow_prob_up

        return {
            "spot_minus_strike": spot_minus_strike,
            "spot_minus_strike_bps": spot_minus_strike_bps,
            "ret_10_bps": ret_10_bps,
            "ret_30_bps": ret_30_bps,
            "breakout_persistence_60s": breakout_persistence,
            "strike_z": strike_z,
            "shadow_score": shadow_score,
            "shadow_prob_up": shadow_prob_up,
            "shadow_prob_down": shadow_prob_down,
        }

    def _shadow_candidate_side(
        self,
        *,
        shadow_prob_up: float,
        shadow_prob_down: float,
        ask_up: Optional[Decimal],
        ask_down: Optional[Decimal],
        time_left_sec: float,
        shadow_score: float,
    ) -> Tuple[Optional[str], Optional[Decimal]]:
        if time_left_sec < self.cfg.min_entry_sec:
            return None, None
        if abs(shadow_score) < self.cfg.shadow_score_min_abs:
            return None, None
        if not (float(self.cfg.min_prob_band) <= shadow_prob_up <= float(self.cfg.max_prob_band)):
            return None, None

        best_side: Optional[str] = None
        best_edge: Optional[Decimal] = None

        if ask_up is not None and ask_up > 0:
            edge_up = Decimal(str(shadow_prob_up)) - ask_up
            if edge_up >= self.cfg.min_edge:
                best_side = "BUY_UP"
                best_edge = edge_up

        if ask_down is not None and ask_down > 0:
            edge_down = Decimal(str(shadow_prob_down)) - ask_down
            if edge_down >= self.cfg.min_edge and (best_edge is None or edge_down > best_edge):
                best_side = "BUY_DOWN"
                best_edge = edge_down

        return best_side, best_edge

    def _snapshot_payload(self, market: Dict[str, Any], slug: str, spot: Decimal, sigma: Decimal) -> Dict[str, Any]:
        base = super()._snapshot_payload(market=market, slug=slug, spot=spot, sigma=sigma)
        strike_val = base.get("strike")
        strike = Decimal(str(strike_val)) if strike_val is not None else None
        signal = self._shadow_signal(
            spot=spot,
            strike=strike,
            sigma=sigma,
            time_left_sec=float(base.get("time_left_sec") or 0.0),
        )
        ask_up = Decimal(str(base["ask_up"])) if base.get("ask_up") is not None else None
        ask_down = Decimal(str(base["ask_down"])) if base.get("ask_down") is not None else None
        candidate_side, candidate_edge = self._shadow_candidate_side(
            shadow_prob_up=float(signal["shadow_prob_up"] or 0.5),
            shadow_prob_down=float(signal["shadow_prob_down"] or 0.5),
            ask_up=ask_up,
            ask_down=ask_down,
            time_left_sec=float(base.get("time_left_sec") or 0.0),
            shadow_score=float(signal["shadow_score"] or 0.0),
        )
        base.update(
            {
                "probe_kind": "shadow_feature",
                "formula": "shadow_prob = 0.5 + 0.49 * weighted(strike_z, ret_10_bps, ret_30_bps, breakout_persistence_60s)",
                "spot_minus_strike": signal["spot_minus_strike"],
                "spot_minus_strike_bps": signal["spot_minus_strike_bps"],
                "ret_10_bps": signal["ret_10_bps"],
                "ret_30_bps": signal["ret_30_bps"],
                "breakout_persistence_60s": signal["breakout_persistence_60s"],
                "strike_z": signal["strike_z"],
                "shadow_score": signal["shadow_score"],
                "shadow_prob_up": signal["shadow_prob_up"],
                "shadow_prob_down": signal["shadow_prob_down"],
                "shadow_min_score_abs": self.cfg.shadow_score_min_abs,
                "candidate_side": candidate_side,
                "candidate_edge": _safe_float(candidate_edge),
                "candidate_edge_kind": "shadow_edge",
            }
        )
        return base

    def _log_candidate(self, payload: Dict[str, Any]) -> None:
        candidate_side = payload.get("candidate_side")
        candidate_edge = payload.get("candidate_edge")
        if not candidate_side or candidate_edge is None:
            self.last_candidate_signature = None
            return
        signature = json.dumps(
            {
                "slug": payload.get("slug"),
                "candidate_side": candidate_side,
                "candidate_edge": round(float(candidate_edge), 4),
                "shadow_score": round(float(payload.get("shadow_score") or 0.0), 4),
            },
            sort_keys=True,
        )
        if signature == self.last_candidate_signature:
            return
        self.last_candidate_signature = signature
        self.db.log_strategy_event(self.run_id, "SHADOW_SIGNAL_CANDIDATE", payload)
        logger.info(
            f"SHADOW candidate slug={payload.get('slug')} side={candidate_side} "
            f"edge={float(candidate_edge):.4f} shadow_score={float(payload.get('shadow_score') or 0.0):+.4f} "
            f"strike_z={float(payload.get('strike_z') or 0.0):+.3f} "
            f"ret10={payload.get('ret_10_bps')} ret30={payload.get('ret_30_bps')}"
        )

    def _log_verbose_snapshot(self, payload: Dict[str, Any]) -> None:
        if not self.cfg.verbose:
            return
        now_ts = time.time()
        if self.last_verbose_ts > 0 and (now_ts - self.last_verbose_ts) < self.cfg.verbose_every_sec:
            return
        self.last_verbose_ts = now_ts
        logger.info(
            "SHADOW snapshot "
            f"slug={payload.get('slug')} "
            f"t_left={float(payload.get('time_left_sec') or 0.0):.1f}s "
            f"spot={float(payload.get('spot') or 0.0):.2f} "
            f"strike={payload.get('strike')} "
            f"shadow_score={float(payload.get('shadow_score') or 0.0):+.4f} "
            f"shadow_up={float(payload.get('shadow_prob_up') or 0.0):.4f} "
            f"ret10={payload.get('ret_10_bps')} "
            f"ret30={payload.get('ret_30_bps')} "
            f"persist60={payload.get('breakout_persistence_60s')} "
            f"candidate={payload.get('candidate_side') or 'NONE'} "
            f"edge={payload.get('candidate_edge')}"
        )

    def _maybe_open_paper_trade(self, payload: Dict[str, Any]) -> None:
        if not self.cfg.paper_trade:
            return
        slug = str(payload.get("slug") or "")
        if not slug or slug in self.paper_positions_by_slug or slug in self.paper_settled_slugs:
            return

        matured = self._candidate_matured_payload(slug)
        if not matured:
            return

        side = str(matured.get("candidate_side") or "")
        if side not in {"BUY_UP", "BUY_DOWN"}:
            return
        entry_price = matured.get("ask_up") if side == "BUY_UP" else matured.get("ask_down")
        if entry_price is None:
            return
        entry_price = float(entry_price)
        if entry_price <= 0:
            return

        paper_id = f"shadow_paper_{slug}"
        paper_payload = {
            "paper_id": paper_id,
            "slug": slug,
            "side": side,
            "entry_price": entry_price,
            "entry_qty": float(self.cfg.paper_entry_qty),
            "entry_ts": _utc_now().isoformat(),
            "candidate_edge": matured.get("candidate_edge"),
            "shadow_score": matured.get("shadow_score"),
            "shadow_prob_up": matured.get("shadow_prob_up"),
            "shadow_prob_down": matured.get("shadow_prob_down"),
            "spot_minus_strike": matured.get("spot_minus_strike"),
            "ret_10_bps": matured.get("ret_10_bps"),
            "ret_30_bps": matured.get("ret_30_bps"),
            "breakout_persistence_60s": matured.get("breakout_persistence_60s"),
            "spot": matured.get("spot"),
            "strike": matured.get("strike"),
            "market_end_ts": matured.get("market_end_ts"),
            "time_left_sec": matured.get("time_left_sec"),
            "paper_persistence_sec": self.cfg.paper_persistence_sec,
            "mode": "shadow_paper_trade",
        }
        self.paper_positions_by_slug[slug] = paper_payload
        self.db.log_strategy_event(self.run_id, "SHADOW_PAPER_TRADE_ENTRY", paper_payload)
        logger.info(
            f"SHADOW paper entry slug={slug} side={side} px={entry_price:.4f} "
            f"score={float(matured.get('shadow_score') or 0.0):+.4f} "
            f"edge={float(matured.get('candidate_edge') or 0.0):.4f}"
        )

    def _settle_paper_trade(self, slug: str) -> None:
        position = self.paper_positions_by_slug.get(slug)
        snapshot = self.latest_snapshot_by_slug.get(slug)
        if not position or not snapshot:
            return
        spot = snapshot.get("spot")
        strike = snapshot.get("strike")
        if spot is None or strike is None:
            return
        spot_f = float(spot)
        strike_f = float(strike)
        outcome = "UP" if spot_f > strike_f else "DOWN"
        side = str(position.get("side") or "")
        entry_price = float(position.get("entry_price") or 0.0)
        won = (side == "BUY_UP" and outcome == "UP") or (side == "BUY_DOWN" and outcome == "DOWN")
        exit_price = 1.0 if won else 0.0
        pnl = exit_price - entry_price
        settle_payload = {
            **position,
            "exit_ts": _utc_now().isoformat(),
            "settlement_spot": spot_f,
            "settlement_strike": strike_f,
            "settlement_outcome": outcome,
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "won": won,
            "close_kind": "settlement",
            "exit_reason": "settlement",
        }
        self.db.log_strategy_event(self.run_id, "SHADOW_PAPER_TRADE_SETTLEMENT", settle_payload)
        logger.info(
            f"SHADOW paper settlement slug={slug} side={side} outcome={outcome} "
            f"entry={entry_price:.4f} exit={exit_price:.4f} pnl={pnl:+.4f}"
        )
        self.paper_settled_slugs.add(slug)
        self.paper_positions_by_slug.pop(slug, None)
        self.candidate_streaks.pop(slug, None)

    def run(self) -> int:
        started_at = time.time()
        self._build_data_node()
        self.db.log_run_start(
            run_id=self.run_id,
            mode="probe",
            test_mode=True,
            maker_mode=False,
            notes={
                "script": "scripts/shadow_feature_probe.py",
                "version": 1,
                "min_edge": float(self.cfg.min_edge),
                "min_entry_sec": self.cfg.min_entry_sec,
                "paper_trade": self.cfg.paper_trade,
                "paper_persistence_sec": self.cfg.paper_persistence_sec,
                "shadow_score_min_abs": self.cfg.shadow_score_min_abs,
                "weights": {
                    "strike_z": self.cfg.strike_z_weight,
                    "ret_10": self.cfg.ret_10_weight,
                    "ret_30": self.cfg.ret_30_weight,
                    "persistence": self.cfg.persistence_weight,
                },
            },
        )
        logger.info(f"Shadow feature probe started run_id={self.run_id}")

        while not self.stop_requested:
            if self.cfg.duration_sec > 0:
                elapsed_sec = time.time() - started_at
                if elapsed_sec >= self.cfg.duration_sec:
                    break

            slug = self._resolve_market()
            if not slug:
                logger.warning("No BTC 15m slug resolved; retrying.")
                time.sleep(self.cfg.interval_sec)
                continue

            market = self._fetch_market(slug)
            if not market:
                logger.warning(f"No market payload for slug={slug}; retrying.")
                time.sleep(self.cfg.interval_sec)
                continue

            if slug != self.current_slug:
                self.current_slug = slug
                strike, strike_source = self._resolve_strike(market=market, slug=slug, latest_spot=None)
                self.db.log_strategy_event(
                    self.run_id,
                    "SHADOW_PROBE_MARKET",
                    {
                        "slug": slug,
                        "question": _extract_question(market),
                        "strike": _safe_float(strike),
                        "strike_source": strike_source,
                    },
                )
                logger.info(f"SHADOW market slug={slug} strike={_safe_float(strike)} source={strike_source}")

            spot = self._fetch_spot()
            if spot is None or spot <= 0:
                logger.warning("Spot fetch unavailable; retrying.")
                time.sleep(self.cfg.interval_sec)
                continue

            sigma = self._estimate_sigma()
            payload = self._snapshot_payload(market=market, slug=slug, spot=spot, sigma=sigma)
            self.latest_snapshot_by_slug[slug] = dict(payload)
            self.db.log_strategy_event(self.run_id, "SHADOW_SIGNAL_SNAPSHOT", payload)
            self._log_verbose_snapshot(payload)
            self._update_candidate_streak(payload)
            self._log_candidate(payload)
            self._maybe_open_paper_trade(payload)
            self._maybe_close_paper_trade(slug, payload)
            self._maybe_settle_paper_trades()
            time.sleep(self.cfg.interval_sec)

        self._maybe_settle_paper_trades(force=True)
        self._shutdown_node()
        self.db.log_run_stop(self.run_id, notes={"stopped_at": _utc_now().isoformat()})
        logger.info(f"Shadow feature probe stopped run_id={self.run_id}")
        return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Independent shadow probe for strike-relative + breakout features")
    ap.add_argument("--db", default="./logs/shadow_probe.db", help="SQLite DB path")
    ap.add_argument("--interval-sec", type=float, default=2.0, help="Polling interval in seconds")
    ap.add_argument("--duration-sec", type=float, default=0.0, help="Run duration in seconds; 0 means until interrupted")
    ap.add_argument("--verbose", action="store_true", help="Print periodic snapshot summaries")
    ap.add_argument("--verbose-every-sec", type=float, default=30.0, help="Minimum seconds between verbose lines")
    ap.add_argument("--min-edge", type=float, default=0.04, help="Minimum shadow edge to flag a candidate")
    ap.add_argument("--min-prob-band", type=float, default=0.08, help="Lower fair probability band for entries")
    ap.add_argument("--max-prob-band", type=float, default=0.92, help="Upper fair probability band for entries")
    ap.add_argument("--min-entry-sec", type=float, default=90.0, help="No new entries below this time remaining")
    ap.add_argument("--reduce-only-sec", type=float, default=30.0, help="Reference threshold for reduction-only window")
    ap.add_argument("--force-flat-sec", type=float, default=15.0, help="Reference threshold for forced flat window")
    ap.add_argument("--sigma-default", type=float, default=0.60, help="Fallback annualized sigma")
    ap.add_argument("--sigma-floor", type=float, default=0.20, help="Minimum annualized sigma")
    ap.add_argument("--sigma-ceiling", type=float, default=1.20, help="Maximum annualized sigma")
    ap.add_argument("--sigma-min-points", type=int, default=20, help="Minimum spot points before realized sigma is trusted")
    ap.add_argument("--sigma-window-points", type=int, default=120, help="Spot history window for realized sigma")
    ap.add_argument("--paper-trade", action="store_true", help="Enable one-paper-trade-per-market simulated entries")
    ap.add_argument("--paper-persistence-sec", type=float, default=10.0, help="Candidate must persist this many seconds before paper entry")
    ap.add_argument("--paper-settle-grace-sec", type=float, default=5.0, help="Wait after market end before paper settlement")
    ap.add_argument("--paper-entry-qty", type=float, default=1.0, help="Simulated entry quantity for paper mode")
    ap.add_argument(
        "--paper-exit-policy",
        choices=("settlement", "simple"),
        default="settlement",
        help="Paper close mode: hold to settlement, or use a simple bid-mark exit policy",
    )
    ap.add_argument("--paper-exit-min-hold-sec", type=float, default=20.0, help="Minimum hold time before simple paper exits")
    ap.add_argument("--paper-take-profit", type=float, default=0.12, help="Simple paper exit take-profit in price points")
    ap.add_argument("--paper-stop-loss", type=float, default=0.08, help="Simple paper exit stop-loss in price points")
    ap.add_argument("--paper-flip-exit-min-score", type=float, default=0.25, help="Minimum abs shadow score to allow flip-confirmed simple exits")
    ap.add_argument("--shadow-score-min-abs", type=float, default=0.15, help="Minimum absolute shadow score before considering entries")
    ap.add_argument("--strike-z-weight", type=float, default=0.55, help="Weight for strike-relative z component")
    ap.add_argument("--ret-10-weight", type=float, default=0.20, help="Weight for 10s return component")
    ap.add_argument("--ret-30-weight", type=float, default=0.15, help="Weight for 30s return component")
    ap.add_argument("--persistence-weight", type=float, default=0.10, help="Weight for breakout persistence component")
    ap.add_argument("--strike-z-scale", type=float, default=1.50, help="tanh scale for strike z-score")
    ap.add_argument("--ret-10-bps-scale", type=float, default=18.0, help="tanh scale for 10s return in bps")
    ap.add_argument("--ret-30-bps-scale", type=float, default=35.0, help="tanh scale for 30s return in bps")
    ap.add_argument("--breakout-window-sec", type=float, default=60.0, help="Window for breakout persistence")
    ap.add_argument("--ret-10-lookback-sec", type=float, default=10.0, help="Lookback for 10s return")
    ap.add_argument("--ret-30-lookback-sec", type=float, default=30.0, help="Lookback for 30s return")
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    cfg = ShadowProbeConfig(
        db_path=args.db,
        interval_sec=max(0.5, float(args.interval_sec)),
        duration_sec=max(0.0, float(args.duration_sec)),
        verbose=bool(args.verbose),
        verbose_every_sec=max(1.0, float(args.verbose_every_sec)),
        min_edge=Decimal(str(args.min_edge)),
        min_prob_band=Decimal(str(args.min_prob_band)),
        max_prob_band=Decimal(str(args.max_prob_band)),
        min_entry_sec=max(0.0, float(args.min_entry_sec)),
        reduce_only_sec=max(0.0, float(args.reduce_only_sec)),
        force_flat_sec=max(0.0, float(args.force_flat_sec)),
        sigma_default=Decimal(str(args.sigma_default)),
        sigma_floor=Decimal(str(args.sigma_floor)),
        sigma_ceiling=Decimal(str(args.sigma_ceiling)),
        sigma_min_points=max(2, int(args.sigma_min_points)),
        sigma_window_points=max(5, int(args.sigma_window_points)),
        paper_trade=bool(args.paper_trade),
        paper_persistence_sec=max(0.0, float(args.paper_persistence_sec)),
        paper_settle_grace_sec=max(0.0, float(args.paper_settle_grace_sec)),
        paper_entry_qty=max(0.0, float(args.paper_entry_qty)),
        paper_exit_policy=str(args.paper_exit_policy),
        paper_exit_min_hold_sec=max(0.0, float(args.paper_exit_min_hold_sec)),
        paper_take_profit=max(0.0, float(args.paper_take_profit)),
        paper_stop_loss=max(0.0, float(args.paper_stop_loss)),
        paper_flip_exit_min_score=max(0.0, float(args.paper_flip_exit_min_score)),
        strike_z_weight=float(args.strike_z_weight),
        ret_10_weight=float(args.ret_10_weight),
        ret_30_weight=float(args.ret_30_weight),
        persistence_weight=float(args.persistence_weight),
        strike_z_scale=max(0.1, float(args.strike_z_scale)),
        ret_10_bps_scale=max(0.1, float(args.ret_10_bps_scale)),
        ret_30_bps_scale=max(0.1, float(args.ret_30_bps_scale)),
        shadow_score_min_abs=max(0.0, float(args.shadow_score_min_abs)),
        breakout_window_sec=max(5.0, float(args.breakout_window_sec)),
        ret_10_lookback_sec=max(1.0, float(args.ret_10_lookback_sec)),
        ret_30_lookback_sec=max(1.0, float(args.ret_30_lookback_sec)),
    )
    total_weight = cfg.strike_z_weight + cfg.ret_10_weight + cfg.ret_30_weight + cfg.persistence_weight
    if total_weight <= 0:
        raise SystemExit("Invalid weights: total must be > 0")
    cfg.strike_z_weight /= total_weight
    cfg.ret_10_weight /= total_weight
    cfg.ret_30_weight /= total_weight
    cfg.persistence_weight /= total_weight

    probe = ShadowFeatureProbe(cfg)
    signal_hits = {"count": 0}

    def _handle_signal(signum: int, _frame: Any) -> None:
        signal_hits["count"] += 1
        logger.info(f"Signal received: {signum}; stopping shadow probe.")
        probe.stop_requested = True
        probe._shutdown_node()
        if signal_hits["count"] >= 2:
            logger.warning("Second interrupt received; forcing exit.")
            os._exit(130)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    return probe.run()


if __name__ == "__main__":
    raise SystemExit(main())
