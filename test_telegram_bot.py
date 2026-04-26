from __future__ import annotations

from datetime import datetime, timezone

from dashboard_state import DashboardState, TradeRecord
from telegram_bot import render_status


class MockNotifier:
    def send_message(self, text: str, parse_mode: str = "MarkdownV2") -> None:
        print(text)


def main() -> None:
    state = DashboardState(
        strike_price=103450.0,
        spot_price=103612.0,
        position_side="UP",
        position_entry=0.62,
        position_qty=8.5,
        position_ask=0.71,
        current_market_price=0.68,
        trades=[
            TradeRecord(1, "btc-15m-103450-1415", "UP", 0.62, 8.5, 0.72, None, True),
        ],
        cumulative_pnl=4.21,
        visible_trades_pnl=4.21,
        usdc_balance=142.60,
        pol_balance=2.34,
        account_last_updated=datetime.now(timezone.utc),
    )

    MockNotifier().send_message(render_status(state))


if __name__ == "__main__":
    main()
