"""TP-exit cycle PnL shape (pre-12-market engineering pass, item D).

btc-updown-15m-1789387200 (LIVE, official UP): BUY 5.634147 UP @0.82 (0.059875
fee shares, so 5.574272 net), maker TP SELL of the full net 5.574272 @0.97.
The journal booked MARKET_CYCLE_PNL +6.47: fill +0.836 plus a zero-cost ghost
settlement of the lagging token balance (+5.634).  The fill-based value is
+0.836.  Root cause and fix: b0c383f (keep the SELL timestamp after a full
exit).  This pins the exact shape on the real fill-ledger + settlement mixins.
"""
import time
from decimal import Decimal

import pytest

from test_cycle_pnl_ledger_regressions import STRIKE, UP_INST, Host


def test_full_tp_exit_with_lagging_balance_is_not_settled_as_free_inventory():
    host = Host(slug="btc-updown-15m-1789387200", onchain_by_token={"111": Decimal("5.634147")})
    host.active_side = host.active_side.UP
    host.fill(UP_INST, "buy", "0.82", "5.634147", fee_shares="0.059875")
    host.recent_buy_fill_ts_by_inst[UP_INST] = time.time() - 400
    realized = host.fill(UP_INST, "sell", "0.97", "5.574272")
    assert realized == pytest.approx(Decimal("0.83614076895024"), abs=Decimal("1e-6"))
    assert Decimal(str(host.live_inventory_cost[UP_INST]["qty"])) == 0

    # ~seconds later the conditional-token balance still reports the pre-SELL holding.
    assert host._get_effective_sellable_qty(UP_INST) == Decimal("0")
    assert not [e for e, _ in host.events if e == "GHOST_INVENTORY_RECONCILED"]

    cycle = host.settle(twap=STRIKE + 150)  # UP wins
    assert cycle["cycle_settlement_pnl_usdc"] == pytest.approx(0.0)
    assert cycle["cycle_combined_pnl_usdc"] == pytest.approx(0.836141, abs=1e-5)  # journal had +6.47
