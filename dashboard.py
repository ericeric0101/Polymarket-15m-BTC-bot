from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dashboard_state import DashboardState, TradeRecord


PANEL_STYLE = "dim white"
PANEL_PADDING = (0, 1)


def _money(value: float, *, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}${value:,.2f}"


def _price(value: Optional[float]) -> str:
    if value is None:
        return "NA"
    return f"{value:.2f}"


def _safe_float(value: Optional[float], default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return default
    return float(value)


def _style_for_signed(value: float) -> str:
    if value > 0:
        return "bright_green"
    if value < 0:
        return "bright_red"
    return "white"


def _side_text(side: Optional[str]) -> Text:
    normalized = str(side or "").upper()
    if normalized == "UP":
        return Text("UP", style="bold bright_green")
    if normalized == "DOWN":
        return Text("DOWN", style="bold bright_red")
    return Text("— NO POSITION —", style="dim")


class BTCDashboard:
    def __init__(self, state: DashboardState, *, console: Optional[Console] = None) -> None:
        self.state = state
        self.console = console or Console(style="white on black")
        self._live: Optional[Live] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._render_lock = threading.RLock()
        self.state.add_listener(self.refresh)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="btc-dashboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def refresh(self) -> None:
        live = self._live
        if live is None:
            return
        with self._render_lock:
            live.update(self.render(), refresh=True)

    def _run(self) -> None:
        with Live(
            self.render(),
            console=self.console,
            refresh_per_second=1,
            screen=True,
            auto_refresh=False,
        ) as live:
            self._live = live
            while not self._stop_event.wait(1.0):
                self.refresh()
            self._live = None

    def render(self) -> Layout:
        state = self.state.snapshot()

        root = Layout(name="root")
        root.split_column(
            Layout(self._market_price_panel(state), name="top", size=7),
            Layout(name="body"),
        )
        root["body"].split_row(
            Layout(name="left", ratio=5),
            Layout(self._recent_trades_panel(state), name="right", ratio=8),
        )
        root["left"].split_column(
            Layout(self._current_position_panel(state), name="position", ratio=5),
            Layout(self._account_summary_panel(state), name="account", ratio=4),
        )
        return root

    def _market_price_panel(self, state: DashboardState) -> Panel:
        spread = state.spot_price - state.strike_price
        spread_text = Text()
        if spread > 0:
            spread_text.append("▲ ", style="bright_green")
            spread_text.append(_money(spread, signed=True), style="bold bright_green")
        elif spread < 0:
            spread_text.append("▼ ", style="bright_red")
            spread_text.append(_money(spread, signed=True), style="bold bright_red")
        else:
            spread_text.append(_money(spread), style="white")

        grid = Table.grid(expand=True)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="center", width=3)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="center", width=3)
        grid.add_column(justify="center", ratio=1)
        grid.add_row(
            Text("STRIKE PRICE", style="dim"),
            Text("│", style="dim"),
            Text("SPOT PRICE", style="dim"),
            Text("│", style="dim"),
            Text("SPREAD", style="dim"),
        )
        grid.add_row(
            Text(_money(state.strike_price), style="bold white"),
            Text("│", style="dim"),
            Text(_money(state.spot_price), style="bold white"),
            Text("│", style="dim"),
            spread_text,
        )

        subtitle = Text(f"updated {state.updated_at.astimezone(timezone.utc):%H:%M:%S UTC}", style="dim")
        return Panel(
            Align.center(grid, vertical="middle"),
            title=Text("BTC 15-MIN MARKET", style="bold bright_blue"),
            subtitle=subtitle,
            subtitle_align="right",
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    def _current_position_panel(self, state: DashboardState) -> Panel:
        side = str(state.position_side or "").upper()
        qty = _safe_float(state.position_qty)
        entry = state.position_entry

        if side not in {"UP", "DOWN"} or entry is None or qty <= 0:
            body = Align.center(_side_text(None), vertical="middle")
            return Panel(
                body,
                title=Text("CURRENT POSITION", style="bold white"),
                border_style=PANEL_STYLE,
                padding=PANEL_PADDING,
            )

        unrealized = (state.current_market_price - entry) * qty
        rows = Table.grid(expand=True)
        rows.add_column(ratio=1)
        rows.add_column(justify="right", ratio=1)

        side_line = Text()
        side_line.append("Side", style="dim")
        side_value = _side_text(side)

        ask_value = Text("NA", style="dim")
        if state.position_ask is not None:
            ask_value = Text(f"{state.position_ask:.2f} USDC", style="bold yellow")

        pnl_value = Text(_money(unrealized, signed=True), style=f"bold {_style_for_signed(unrealized)}")
        rows.add_row(side_line, side_value)
        rows.add_row(Text("Entry Price", style="dim"), Text(f"{entry:.2f} USDC", style="bold white"))
        rows.add_row(Text("Target Ask", style="dim"), ask_value)
        rows.add_row(Text("Quantity", style="dim"), Text(f"{qty:.2f} shares", style="bold white"))
        rows.add_row(Text("Mkt price", style="dim"), Text(f"{state.current_market_price:.2f} USDC", style="white"))
        rows.add_row(Text("Unrealized PnL", style="dim"), pnl_value)

        return Panel(
            rows,
            title=Text("CURRENT POSITION", style="bold white"),
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    def _account_summary_panel(self, state: DashboardState) -> Panel:
        pnl_style = f"bold {_style_for_signed(state.cumulative_pnl)}"

        pnl_box = Panel(
            Group(
                Text("Cumulative PnL", style="dim"),
                Text(_money(state.cumulative_pnl, signed=True), style=pnl_style),
            ),
            border_style="dim",
            padding=(0, 1),
        )
        usdc_box = Panel(
            Group(
                Text("USDC Balance", style="dim"),
                Text(_money(state.usdc_balance), style="bold white"),
            ),
            border_style="dim",
            padding=(0, 1),
        )
        pol_box = Panel(
            Group(
                Text("POL Balance", style="dim"),
                Text(f"{state.pol_balance:.4f} gas reserve", style="bold white"),
            ),
            border_style="dim",
            padding=(0, 1),
        )
        balances_grid = Table.grid(expand=True)
        balances_grid.add_column(ratio=1)
        balances_grid.add_column(ratio=1)
        balances_grid.add_row(usdc_box, pol_box)

        last_updated = state.account_last_updated.astimezone(timezone.utc).strftime("%H:%M UTC")
        return Panel(
            Group(pnl_box, balances_grid),
            title=Text("ACCOUNT SUMMARY", style="bold white"),
            subtitle=Text(f"last updated: {last_updated}", style="dim"),
            subtitle_align="left",
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    def _recent_trades_panel(self, state: DashboardState) -> Panel:
        table = Table(
            expand=True,
            show_lines=False,
            show_edge=False,
            header_style="bold dim",
            pad_edge=False,
        )
        table.add_column("#", style="dim", justify="right", no_wrap=True)
        table.add_column("Market Slug", style="white", overflow="fold")
        table.add_column("Side", justify="center", no_wrap=True)
        table.add_column("Entry", justify="right", no_wrap=True)
        table.add_column("Exit", justify="right", no_wrap=True)
        table.add_column("Redeem", justify="right", no_wrap=True)
        table.add_column("PnL", justify="right", no_wrap=True)

        if not state.trades:
            table.add_row(Text("—", style="dim"), Text("No trades yet", style="dim"), "", "", "", "", "")
        else:
            for idx, trade in enumerate(state.trades[:10], start=1):
                table.add_row(
                    Text(str(idx), style="dim"),
                    Text(trade.market_slug, style="bright_blue"),
                    _side_text(trade.side),
                    Text(_price(trade.entry_price), style="white"),
                    self._exit_cell(trade),
                    self._redeem_cell(trade),
                    self._pnl_cell(trade),
                )

        return Panel(
            table,
            title=Text("RECENT TRADES", style="bold white"),
            subtitle=Text("last 10", style="dim"),
            subtitle_align="right",
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    @staticmethod
    def _exit_cell(trade: TradeRecord) -> Text:
        if trade.exit_price is None:
            return Text("NA", style="dim")
        return Text(f"{trade.exit_price:.2f}", style="white")

    @staticmethod
    def _redeem_cell(trade: TradeRecord) -> Text:
        if trade.exit_price is not None and trade.redeem_amount is None:
            return Text("NA", style="dim")
        if not trade.is_settled:
            return Text("—", style="dim")
        if trade.redeem_amount is None:
            return Text("pending", style="yellow")
        return Text(_money(trade.redeem_amount), style="bold bright_white")

    @staticmethod
    def _pnl_cell(trade: TradeRecord) -> Text:
        entry_cost = trade.entry_price * trade.qty
        realized_value = None
        if trade.exit_price is not None:
            realized_value = trade.exit_price * trade.qty
        elif trade.redeem_amount is not None:
            realized_value = trade.redeem_amount

        if realized_value is None:
            return Text("—", style="dim")

        pnl = realized_value - entry_cost
        return Text(_money(pnl, signed=True), style=f"bold {_style_for_signed(pnl)}")


def _mock_state() -> DashboardState:
    return DashboardState(
        strike_price=103450.00,
        spot_price=103612.34,
        position_side="UP",
        position_entry=0.62,
        position_qty=8.5,
        position_ask=0.71,
        current_market_price=0.68,
        trades=[
            TradeRecord(10, "btc-15m-103450-1415", "UP", 0.62, 8.5, None, None, False),
            TradeRecord(9, "btc-15m-103200-1400", "UP", 0.58, 8.5, None, None, True),
            TradeRecord(8, "btc-15m-103100-1345", "UP", 0.61, 8.5, None, 5.12, True),
            TradeRecord(7, "btc-15m-103300-1330", "DOWN", 0.44, 8.5, 0.39, None, True),
            TradeRecord(6, "btc-15m-103000-1315", "UP", 0.55, 8.5, None, 4.75, True),
            TradeRecord(5, "btc-15m-102800-1300", "DOWN", 0.48, 8.5, None, 5.30, True),
            TradeRecord(4, "btc-15m-103100-1245", "UP", 0.63, 5.0, None, None, True),
            TradeRecord(3, "btc-15m-102900-1230", "UP", 0.57, 8.5, 0.61, None, True),
            TradeRecord(2, "btc-15m-102700-1215", "DOWN", 0.51, 5.1, None, 0.0, True),
            TradeRecord(1, "btc-15m-103050-1200", "UP", 0.59, 5.0, None, 5.0, True),
        ],
        cumulative_pnl=18.43,
        usdc_balance=142.60,
        pol_balance=2.341,
        account_last_updated=datetime.now(timezone.utc),
    )


if __name__ == "__main__":
    state = _mock_state()
    dashboard = BTCDashboard(state)
    dashboard.start()
    try:
        tick = 0
        while True:
            time.sleep(1.0)
            tick += 1
            spot = 103612.34 + math.sin(tick / 4.0) * 32.0
            market_price = 0.68 + math.sin(tick / 5.0) * 0.03
            state.update(
                spot_price=spot,
                current_market_price=market_price,
                position_ask=round(min(0.99, market_price + 0.03), 2),
            )
            if tick == 8:
                state.upsert_redeem("btc-15m-103200-1400", 6.05)
    except KeyboardInterrupt:
        dashboard.stop()
