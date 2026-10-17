import importlib.util
import pathlib

import numpy as np
import pytest

_P = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "fr1_analyze.py"
_s = importlib.util.spec_from_file_location("fr1_analyze", _P)
fa = importlib.util.module_from_spec(_s)
_s.loader.exec_module(fa)
N = fa.N_WEEKS


def coin(hl_week, bn_week, basis=None):
    return {"hl_sum": np.full(N, hl_week), "bn_sum": np.full(N, bn_week),
            "basis": np.zeros(N + 1) if basis is None else basis, "ok": np.ones(N, dtype=bool)}


def test_short_the_higher_funding_venue_and_pay_costs_once():
    # HL pays 0.1%/week more than Binance -> ~5.2% annualized spread, above theta 5%.
    c = {"X": coin(0.0015, 0.0005)}
    pnl, units, by = fa.simulate(c, 0.05, range(1, 11))
    leg = 0.00045 + 0.0005 + 2 * 0.0002
    assert units[1:11].sum() == 10
    assert by["X"] == pytest.approx(10 * 0.001 - 2 * leg)      # open once, close at the end
    assert fa.annualized(pnl, units, range(1, 11)) == pytest.approx((10 * 0.001 - 2 * leg) / 10 * fa.WEEKS_PER_YEAR)


def test_reverse_direction_and_basis_residual():
    b = np.zeros(N + 1)
    b[2] = 0.001                      # HL richer vs BN during week 1 -> hurts a long-HL position? no: d=-1 is long HL
    c = {"X": coin(0.0005, 0.0015, b)}  # Binance higher -> d = -1 (long HL, short BN)
    pnl, units, _ = fa.simulate(c, 0.05, range(1, 2))
    leg = 0.00045 + 0.0005 + 2 * 0.0002
    # funding: -1 * (0.0005 - 0.0015) = +0.001; basis: -(-1) * (0.001 - 0) = +0.001 (long HL gains)
    assert pnl[1] == pytest.approx(0.001 + 0.001 - 2 * leg)


def test_below_theta_stays_flat_and_missing_week_closes():
    c = {"X": coin(0.0006, 0.0005)}   # 0.52% annualized < 5%
    pnl, units, _ = fa.simulate(c, 0.05, range(1, 5))
    assert units.sum() == 0 and pnl.sum() == 0
    d = coin(0.0015, 0.0005)
    d["ok"][3] = False
    pnl, units, by = fa.simulate({"X": d}, 0.05, range(1, 6))
    assert units[1:6].tolist() == [1, 1, 0, 0, 1]             # weeks 3 (missing) and 4 (signal week missing) flat


def test_block_bootstrap_ci_brackets_constant_return():
    pnl = np.zeros(N); units = np.zeros(N)
    pnl[1:26] = 0.001; units[1:26] = 1
    lo, hi = fa.block_bootstrap(pnl, units, range(1, 26), n=500)
    assert lo == pytest.approx(0.001 * fa.WEEKS_PER_YEAR) and hi == pytest.approx(0.001 * fa.WEEKS_PER_YEAR)
