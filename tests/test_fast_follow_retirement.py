"""Current Phase A runtime contract; no override of the retired execution gate."""
import ast
import asyncio
import inspect
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from bot.market_runtime import handle_order_book_deltas
from test_pricing_runtime_volatility import _DepthHost

@pytest.mark.parametrize('age,accepted',[(1.99,True),(2.01,False)])
def test_neutral_l2_stamp_preserves_exact_maker_delivery_threshold(monkeypatch,age,accepted):
    monkeypatch.setattr(time,'time',lambda:1000.0)
    host=_DepthHost()
    # Market-data callback owns the only timestamp map, with no FF object.
    handle_order_book_deltas(host,SimpleNamespace(instrument_id='UP'))
    assert host.l2_update_ts_by_inst=={'UP':1000.0}
    assert not hasattr(host,'fast_follow_l2_update_ts_by_inst')
    host.l2_update_ts_by_inst['UP']-=age
    bids,asks=asyncio.run(host._get_orderbook_levels_for_instrument('UP'))
    assert (bids is not None and asks is not None) is accepted
    assert host.quote_max_delivery_delay_sec==2.0
