"""MARKET_CYCLE_PNL ledger regressions replayed from the live journal (hermetic).

btc-updown-15m-1789745400: full SELL exit, then a lagging conditional-token
balance was "recovered" as ghost inventory at zero cost and settled as free
shares (journal +6.76 vs fills +0.92).

btc-updown-15m-1790568000: restart mid-market while holding DOWN; startup
rehydration read only the empty framework cache, so settlement booked a flat
cycle (journal 0.00 vs -6.60).
"""
import re
import time
from decimal import Decimal

import pytest

from bot.enums import ActiveSide
from bot.fill_ledger import FillLedgerMixin
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.pricing_runtime import PricingRuntimeMixin
from bot.recovery import StrategyRecoveryMixin
from monitoring.trade_journal_db import TradeJournalDB

STRIKE = 80000.0
UP_INST = "0xcond-111.POLYMARKET"
DOWN_INST = "0xcond-222.POLYMARKET"


class Host(FillLedgerMixin, PricingRuntimeMixin, StrategyRecoveryMixin, StrategyLifecycleMixin):
    """Real fill-ledger, sellable-qty, recovery and settlement code; I/O is in memory."""

    def __init__(self, *, slug, onchain_by_token=None, trade_db=None, live=True):
        self.current_market_slug = slug
        self.current_market_instruments = [UP_INST, DOWN_INST]
        self.instrument_id = UP_INST
        self.market_strike_cache_by_slug = {slug: Decimal(str(STRIKE))}
        self.latest_external_spot = None
        self.last_external_spot = None
        self.inventory_delta_shares = Decimal("0")
        self.live_inventory_cost = {}
        self.market_cycle_realized_net_usdc = Decimal("0")
        self.recent_market_combined_pnls = []
        self.active_side = ActiveSide.DOWN
        self._cycle_total_trades = self._cycle_total_wins = 0
        self.terminal_dashboard = None
        self.recent_buy_fill_ts_by_inst = {}
        self.recent_sell_fill_ts_by_inst = {}
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
        self.sell_delay_after_buy_sec = 0
        self.sellable_after_buy_buffer_shares = Decimal("0")
        self.conditional_balance_safety_buffer_pct = Decimal("0.02")
        self._sell_recovery_venue_cap_by_inst = {}
        self._startup_rehydrated_inventory_force_sell_only = False
        self.onchain_by_token = dict(onchain_by_token or {})
        self.trade_db = trade_db
        self.test_mode = not live
        self.events = []

    # --- collaborators -------------------------------------------------------------------------
    def _is_dry_run_mode(self):
        return self.test_mode

    def _instrument_key(self, inst):
        return str(inst) if inst is not None else ""

    def _normalize_instrument_id(self, inst):
        return inst

    def _normalize_side_text(self, side):
        return str(side or "").lower()

    def _side_for_instrument_id(self, inst):
        return {UP_INST: ActiveSide.UP, DOWN_INST: ActiveSide.DOWN}.get(str(inst), ActiveSide.NONE)

    @staticmethod
    def _extract_token_id_from_instrument(instrument_id):
        m = re.search(r"-([0-9]+)\.POLYMARKET$", str(instrument_id))
        return m.group(1) if m else None

    def _get_conditional_balance_for_token(self, token_id=None, force_refresh=False):
        return self.onchain_by_token.get(token_id)

    def _get_sellable_qty_for_current_instrument(self, instrument_id=None):
        return Decimal("0")  # framework cache has no positions (restart / Polymarket adapter)

    def _clear_profit_run_state(self, inst):
        return None

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))

    def _append_cycle_and_maybe_trigger_regime_guard(self, **kwargs):
        return None

    def _update_terminal_dashboard_snapshot(self):
        return None

    # --- helpers -------------------------------------------------------------------------------
    def fill(self, inst, side, price, qty, fee_shares="0"):
        realized = self._update_live_inventory_cost_from_fill(
            instrument_id=inst, side=side, fill_price=Decimal(price), fill_qty=Decimal(qty),
            fee_usdc=Decimal("0"), fee_shares=Decimal(fee_shares),
        )
        net = Decimal(qty) - Decimal(fee_shares)
        self.inventory_delta_shares += net if side == "buy" else -Decimal(qty)
        if realized is not None:
            self.market_cycle_realized_net_usdc += realized
        return realized

    def settle(self, twap):
        self._polymarket_chainlink_twap_price = twap
        self._polymarket_chainlink_twap_observation_ts = time.time() - 1.0
        self._polymarket_chainlink_twap_window_sec = 60
        self._record_market_settlement()
        return next(p for e, p in self.events if e == "MARKET_CYCLE_PNL")


def test_full_sell_exit_then_lagging_balance_is_not_settled_as_free_inventory():
    """btc-updown-15m-1789745400: fast-follow BUY, maker SELL of the full net qty, DOWN wins."""
    host = Host(slug="btc-updown-15m-1789745400", onchain_by_token={"222": Decimal("5.839507")})
    host.fill(DOWN_INST, "buy", "0.81", "5.839507", fee_shares="0.0647064091656")
    host.recent_buy_fill_ts_by_inst[DOWN_INST] = time.time() - 300  # BUY was ~5 min earlier
    realized = host.fill(DOWN_INST, "sell", "0.97", "5.7748005908344")
    assert realized == pytest.approx(Decimal("0.923968094533504"))

    # ~2.7 s later the quote loop reads the conditional-token balance, which
    # still reports the pre-SELL holding.
    sellable = host._get_effective_sellable_qty(DOWN_INST)

    assert sellable == Decimal("0")
    assert not [e for e, _ in host.events if e == "GHOST_INVENTORY_RECONCILED"]
    assert Decimal(str(host.live_inventory_cost[DOWN_INST]["qty"])) == 0
    cycle = host.settle(twap=STRIKE - 200)
    assert cycle["cycle_settlement_pnl_usdc"] == pytest.approx(0.0)
    assert cycle["cycle_combined_pnl_usdc"] == pytest.approx(0.923968, abs=1e-6)


def test_balance_still_above_ledger_after_grace_is_force_refreshed_before_ghost_recovery():
    host = Host(slug="btc-updown-15m-1789745400", onchain_by_token={"222": Decimal("5.839507")})
    host.fill(DOWN_INST, "buy", "0.81", "5.839507", fee_shares="0.0647064091656")
    host.recent_buy_fill_ts_by_inst[DOWN_INST] = time.time() - 300
    host.fill(DOWN_INST, "sell", "0.97", "5.7748005908344")
    host.recent_sell_fill_ts_by_inst[DOWN_INST] = time.time() - 30  # past the 8 s grace
    refreshes = []

    def balance(token_id=None, force_refresh=False):
        refreshes.append(force_refresh)
        return Decimal("0.0647") if force_refresh else Decimal("5.839507")

    host._get_conditional_balance_for_token = balance
    assert host._get_effective_sellable_qty(DOWN_INST) == Decimal("0")
    assert True in refreshes
    assert not [e for e, _ in host.events if e == "GHOST_INVENTORY_RECONCILED"]


def test_mid_market_restart_rehydrates_held_inventory_from_onchain_balance(tmp_path):
    """btc-updown-15m-1790568000: BUY 10 DOWN @0.66, restart, official UP."""
    db = TradeJournalDB(str(tmp_path / "journal.db"))
    db.log_order_event(run_id="run_before_restart", event_type="ORDER_FILLED", side="BUY", price=0.66,
                       qty=10.0, status="FILLED", instrument_id=DOWN_INST, commission_usdc=0.0,
                       payload={"effective_fee_shares": 0.0})
    host = Host(slug="btc-updown-15m-1790568000", onchain_by_token={"222": Decimal("10")}, trade_db=db)

    host._rehydrate_inventory_state_on_startup()

    state = host.live_inventory_cost[DOWN_INST]
    assert state["qty"] == Decimal("10") and state["avg_entry_price"] == Decimal("0.66")
    assert host.inventory_delta_shares == Decimal("10")
    rehydrated = next(p for e, p in host.events if e == "STARTUP_INVENTORY_REHYDRATED")
    assert rehydrated["legs"][0]["qty_source"] == "onchain_balance"
    host.active_side = ActiveSide.UP
    cycle = host.settle(twap=STRIKE + 85)
    assert cycle["cycle_settlement_pnl_usdc"] == pytest.approx(-6.60)
    assert cycle["cycle_combined_pnl_usdc"] == pytest.approx(-6.60)


def test_dry_run_restart_never_adopts_wallet_balance(tmp_path):
    host = Host(slug="btc-updown-15m-1790568000", onchain_by_token={"222": Decimal("10")},
                trade_db=TradeJournalDB(str(tmp_path / "journal.db")), live=False)
    host._rehydrate_inventory_state_on_startup()
    assert host.live_inventory_cost == {}
    assert host.inventory_delta_shares == Decimal("0")


def test_restart_ignores_sub_share_fee_dust(tmp_path):
    host = Host(slug="btc-updown-15m-1789745400", onchain_by_token={"222": Decimal("0.0647")},
                trade_db=TradeJournalDB(str(tmp_path / "journal.db")))
    host._rehydrate_inventory_state_on_startup()
    assert host.live_inventory_cost == {}
