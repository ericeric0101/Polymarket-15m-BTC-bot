from __future__ import annotations

from typing import Any

from bot.polymarket_data_api import DATA_API_V2_BASE_URL, v2_next_cursor, v2_rows
from bot.smart_money import SmartMoneyConfig, SmartMoneyTracker


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> _Response:
        self.requests.append((path, params))
        return _Response(self.responses.pop(0))


def test_v2_helpers_require_the_documented_data_envelope_and_cursor():
    payload = {"data": [{"id": "one"}], "pagination": {"next_cursor": "opaque-next"}}

    assert DATA_API_V2_BASE_URL == "https://data-api.polymarket.com/v2"
    assert v2_rows(payload) == [{"id": "one"}]
    assert v2_next_cursor(payload) == "opaque-next"
    assert v2_rows([{"id": "old-v1-shape"}]) == []
    assert v2_next_cursor({"data": []}) is None


def test_smart_money_reads_v2_trade_fields_and_condition_filter():
    tracker = SmartMoneyTracker(SmartMoneyConfig(trades_limit=2, min_cash_filter=10.0))
    client = _Client(
        [
            {
                "data": [
                    {
                        "outcome": "Up",
                        "token_id": "token-up",
                        "price": "0.61",
                        "size": "20",
                        "timestamp": "123",
                        "transaction_hash": "0xtx",
                        "proxy_wallet": "0xWallet",
                        "side": "BUY",
                    }
                ]
            }
        ]
    )

    rows = tracker._fetch_trades(client=client, condition_id="0xcondition", token_map={})

    assert client.requests == [
        (
            "/trades",
            {
                "condition": "0xcondition",
                "side": "BUY",
                "taker_only": "false",
                "limit": 2,
                "filter_type": "CASH",
                "filter_amount": 10.0,
            },
        )
    ]
    assert len(rows) == 1
    assert rows[0].asset == "token-up"
    assert rows[0].proxy_wallet == "0xwallet"
    assert rows[0].transaction_hash == "0xtx"


def test_smart_money_reads_v2_position_fields_for_hedger_detection():
    tracker = SmartMoneyTracker(SmartMoneyConfig(position_limit=2, hedge_ratio=0.2))
    client = _Client(
        [
            {
                "data": [
                    {"proxy_wallet": "0xHedge", "outcome": "Up", "current_value": "25"},
                    {"proxy_wallet": "0xHedge", "outcome": "Down", "current_value": "20"},
                ]
            }
        ]
    )

    hedgers = tracker._fetch_hedgers(client=client, condition_id="0xcondition")

    assert client.requests == [
        ("/positions", {"condition": "0xcondition", "status": "OPEN", "limit": 2})
    ]
    assert hedgers == {"0xhedge"}
