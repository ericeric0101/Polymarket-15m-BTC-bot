"""Settlement mechanics rebuilt from the standalone recorder only (no bot exports).

Shared by evaluate_v2.py and the mechanics validation. All functions are as-of:
nothing received after ``decision_ts`` is used.
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

YR_SEC = 365 * 86400
TWAP_WINDOW = 60


def load_parquet_dir(root: Path) -> pd.DataFrame:
    files = sorted(glob.glob(str(Path(root) / "*" / "*.parquet")))
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame()


class ChainlinkSeries:
    """Chainlink spot and TWAP keyed by source second, with local receipt times."""

    def __init__(self, prices: pd.DataFrame, *, window_offset: int) -> None:
        # window_offset: the settlement window for TWAP stamped at second S is
        # seconds [S + window_offset, S + window_offset + 59]  (validated empirically).
        self.window_offset = int(window_offset)
        cl = prices[prices.source.isin(["chainlink_spot", "chainlink_twap60"])].dropna(subset=["last_source_ms"])
        cl = cl.assign(s=(cl.last_source_ms // 1000).astype("int64"), rcv=cl.last_received_ms / 1000.0)
        cl = cl.sort_values("rcv").drop_duplicates(["source", "s"], keep="last")
        spot = cl[cl.source.eq("chainlink_spot")].set_index("s").sort_index()
        twap = cl[cl.source.eq("chainlink_twap60")].set_index("s").sort_index()
        self.spot_px, self.spot_rcv = spot.close, spot.rcv
        self.twap_px, self.twap_rcv = twap.close, twap.rcv

    def window(self, end_s: int) -> range:
        """Source seconds averaged by the TWAP stamped at ``end_s``."""
        first = end_s + self.window_offset
        return range(first, first + TWAP_WINDOW)

    def strike(self, market_start: int, decision_ts: float) -> float | None:
        """Price to Beat = TWAP stamped at the market start, if received by the decision."""
        if market_start in self.twap_px.index and self.twap_rcv[market_start] <= decision_ts:
            return float(self.twap_px[market_start])
        return None

    def known_spot(self, decision_ts: float) -> pd.Series:
        return self.spot_px[self.spot_rcv <= decision_ts]

    def required_future_avg(self, market_end: int, strike: float, decision_ts: float,
                            max_missing: int = 3) -> tuple[float | None, int, int]:
        """(required average of the unobserved window seconds to tie, remaining count, missing filled).

        Observed = window seconds up to the latest Chainlink second received by the
        decision. Gaps inside the observed part are forward-filled; more than
        ``max_missing`` gaps -> unavailable.
        """
        known = self.known_spot(decision_ts)
        if known.empty:
            return None, 0, 0
        last_s = int(known.index.max())
        win = list(self.window(market_end))
        observed = [s for s in win if s <= last_s]
        remaining = len(win) - len(observed)
        if remaining <= 0:
            return None, 0, 0
        filled = known.reindex(range(min(win[0], last_s) - 5, last_s + 1)).ffill()
        vals = filled.reindex(observed)
        missing = int(known.reindex(observed).isna().sum())
        if missing > max_missing or vals.isna().any():
            return None, remaining, missing
        need = (TWAP_WINDOW * strike - float(vals.sum())) / remaining
        return need, remaining, missing

    def determined_settlement(self, market_end: int, decision_ts: float) -> float | None:
        """Settlement TWAP if every window second has been received by the decision, else None."""
        known = self.known_spot(decision_ts)
        vals = known.reindex(list(self.window(market_end)))
        return float(vals.mean()) if len(vals) and vals.notna().all() else None

    def sigma(self, decision_ts: float, lookback: int = 300, min_returns: int = 240) -> float | None:
        """Annualised std of 1 s log returns over the trailing lookback (as-of the decision)."""
        known = self.known_spot(decision_ts)
        if known.empty:
            return None
        last_s = int(known.index.max())
        px = known.reindex(range(last_s - lookback, last_s + 1))
        r = np.log(px).diff().dropna()
        if len(r) < min_returns:
            return None
        sd = float(r.std(ddof=1))
        return sd * np.sqrt(YR_SEC) if sd > 0 else None

    def spot_asof(self, decision_ts: float, max_age: float = 3.0) -> float | None:
        known = self.spot_rcv[self.spot_rcv <= decision_ts]
        if known.empty or decision_ts - float(known.iloc[-1]) > max_age:
            return None
        return float(self.spot_px[known.index[-1]])

    def settlement(self, market_end: int) -> float | None:
        """Reconstructed settlement TWAP (official TWAP tick at end if recorded, else spot mean)."""
        if market_end in self.twap_px.index:
            return float(self.twap_px[market_end])
        vals = self.spot_px.reindex(list(self.window(market_end)))
        return float(vals.mean()) if vals.notna().all() else None


def validate_window(series_prices: pd.DataFrame, offsets=range(-62, 2)) -> pd.DataFrame:
    """Error (bps) between each recorded TWAP tick and the spot mean for candidate window offsets."""
    rows = []
    for off in offsets:
        cs = ChainlinkSeries(series_prices, window_offset=off)
        errs = []
        for s, v in cs.twap_px.items():
            vals = cs.spot_px.reindex(list(cs.window(int(s))))
            if vals.notna().all():
                errs.append((vals.mean() - v) / v * 1e4)
        e = np.abs(np.array(errs)) if errs else np.array([])
        rows.append({"window_offset": off, "n": len(e),
                     "median_abs_bps": float(np.median(e)) if len(e) else None,
                     "p99_abs_bps": float(np.quantile(e, .99)) if len(e) else None,
                     "max_abs_bps": float(e.max()) if len(e) else None})
    return pd.DataFrame(rows)
