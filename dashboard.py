from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.align import Align
from rich.console import Console, Group, Screen
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dashboard_state import DashboardState, TradeRecord
from bot.journal_path import resolve_trade_db_path
from monitoring.pnl_attribution import load_effective_market_pnl, summarize_effective_pnl

RECENT_TRADE_LIMIT = 20
EFFECTIVE_PNL_REFRESH_SEC = 10.0
BASIS_MARK = {
    "FILLS_FINAL": "=", "CASH_CONFIRMED": "=", "OUTCOME_CONFIRMED": "=",
    "ESTIMATED": "~", "PENDING": "?", "OPEN": "", "INCOMPLETE": "!",
}


PANEL_PADDING = (0, 1)


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_ENV_VALUES = _parse_dotenv(Path.cwd() / ".env")
DASHBOARD_THEME = (
    os.getenv("DASHBOARD_THEME") or _ENV_VALUES.get("DASHBOARD_THEME") or "light"
).strip().lower()

if DASHBOARD_THEME == "dark":
    PANEL_STYLE = "dim white"
    CONSOLE_STYLE = "white on black"
    TITLE_STYLE = "bold bright_blue"
    LABEL_STYLE = "dim"
    MUTED_STYLE = "dim"
    TEXT_STYLE = "white"
    VALUE_STYLE = "bold white"
    LINK_STYLE = "bright_blue"
    POS_STYLE = "bright_green"
    NEG_STYLE = "bright_red"
    WARN_STYLE = "yellow"
    SOLD_STYLE = "bright_cyan"
    OPEN_STYLE = "bright_blue"
else:
    PANEL_STYLE = "grey35"
    CONSOLE_STYLE = "black on white"
    TITLE_STYLE = "bold blue"
    LABEL_STYLE = "grey23"
    MUTED_STYLE = "grey50"
    TEXT_STYLE = "black"
    VALUE_STYLE = "bold black"
    LINK_STYLE = "blue"
    POS_STYLE = "green4"
    NEG_STYLE = "red3"
    WARN_STYLE = "dark_goldenrod"
    SOLD_STYLE = "cyan4"
    OPEN_STYLE = "purple4"


def _resolve_db_path(explicit: Optional[str] = None) -> Path:
    # The bot's own resolver: explicit > shell > .env > profile > default.
    return resolve_trade_db_path(explicit)


def _json_loads(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_dt(raw: Optional[str]) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def _optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _style_for_signed(value: float) -> str:
    if value > 0:
        return POS_STYLE
    if value < 0:
        return NEG_STYLE
    return TEXT_STYLE


def _format_duration(value: Optional[float]) -> str:
    if value is None:
        return "NA"
    seconds = max(0, int(value))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _countdown_time_left(state: DashboardState) -> Optional[float]:
    if state.time_left_sec is None:
        return None
    if state.decision_updated_at is None:
        return state.time_left_sec
    elapsed = (datetime.now(timezone.utc) - state.decision_updated_at.astimezone(timezone.utc)).total_seconds()
    return max(0.0, state.time_left_sec - elapsed)


def _shorten(value: Optional[str], max_len: int) -> str:
    if not value:
        return "NA"
    value = " ".join(str(value).split())
    if len(value) <= max_len:
        return value
    return value[: max(0, max_len - 1)] + "…"


def _slug_epoch(slug: str) -> Optional[int]:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def _side_text(side: Optional[str]) -> Text:
    normalized = str(side or "").upper()
    if normalized == "UP":
        return Text("UP", style=f"bold {POS_STYLE}")
    if normalized == "DOWN":
        return Text("DOWN", style=f"bold {NEG_STYLE}")
    return Text("— NO POSITION —", style=MUTED_STYLE)


class BTCDashboard:
    def __init__(self, state: DashboardState, *, console: Optional[Console] = None) -> None:
        self.state = state
        self.console = console or Console(style=CONSOLE_STYLE)
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
            live.update(self.render_screen(), refresh=True)

    def _run(self) -> None:
        with Live(
            self.render_screen(),
            console=self.console,
            refresh_per_second=1,
            screen=True,
            auto_refresh=False,
        ) as live:
            self._live = live
            while not self._stop_event.wait(1.0):
                self.refresh()
            self._live = None

    def render_screen(self) -> Screen:
        return Screen(self.render(), style=CONSOLE_STYLE)

    def render(self) -> Layout:
        state = self.state.snapshot()

        root = Layout(name="root")
        root.split_column(
            Layout(self._market_price_panel(state), name="top", size=5),
            Layout(name="body"),
        )
        root["body"].split_row(
            Layout(name="left", ratio=5),
            Layout(name="right", ratio=8),
        )
        root["left"].split_column(
            Layout(self._current_position_panel(state), name="position", ratio=5),
            Layout(self._account_summary_panel(state), name="account", ratio=4),
        )
        root["right"].split_column(
            Layout(self._recent_trades_panel(state), name="recent", ratio=7),
            Layout(self._bot_decision_panel(state), name="decision", ratio=3),
        )
        return root

    def _market_price_panel(self, state: DashboardState) -> Panel:
        spread = state.spot_price - state.strike_price
        spread_text = Text()
        if spread > 0:
            spread_text.append("▲ ", style=POS_STYLE)
            spread_text.append(_money(spread, signed=True), style=f"bold {POS_STYLE}")
        elif spread < 0:
            spread_text.append("▼ ", style=NEG_STYLE)
            spread_text.append(_money(spread, signed=True), style=f"bold {NEG_STYLE}")
        else:
            spread_text.append(_money(spread), style=TEXT_STYLE)

        values = Table.grid(expand=True)
        values.add_column(justify="center", ratio=1, min_width=22)
        values.add_column(justify="center", width=3)
        values.add_column(justify="center", ratio=1, min_width=22)
        values.add_column(justify="center", width=3)
        values.add_column(justify="center", ratio=1, min_width=22)

        strike_cell = Table.grid(expand=True)
        strike_cell.add_column(justify="center")
        strike_cell.add_row(Text("STRIKE PRICE", style=LABEL_STYLE, no_wrap=True))
        strike_cell.add_row(Text(_money(state.strike_price), style=VALUE_STYLE, no_wrap=True))

        spot_cell = Table.grid(expand=True)
        spot_cell.add_column(justify="center")
        spot_cell.add_row(Text("SPOT PRICE", style=LABEL_STYLE, no_wrap=True))
        spot_cell.add_row(Text(_money(state.spot_price), style=VALUE_STYLE, no_wrap=True))

        spread_cell = Table.grid(expand=True)
        spread_cell.add_column(justify="center")
        spread_cell.add_row(Text("SPREAD", style=LABEL_STYLE, no_wrap=True))
        spread_text.no_wrap = True
        spread_cell.add_row(spread_text)

        values.add_row(
            strike_cell,
            Text("│", style=MUTED_STYLE),
            spot_cell,
            Text("│", style=MUTED_STYLE),
            spread_cell,
        )

        return Panel(
            Align.center(values, vertical="middle"),
            title=Text("BTC 15-MIN MARKET", style=TITLE_STYLE),
            subtitle=Text(f"updated {state.updated_at.astimezone(timezone.utc):%H:%M:%S UTC}", style=MUTED_STYLE),
            subtitle_align="right",
            border_style=PANEL_STYLE,
            padding=(0, 2),
        )

    def _current_position_panel(self, state: DashboardState) -> Panel:
        side = str(state.position_side or "").upper()
        qty = _safe_float(state.position_qty)
        entry = state.position_entry

        if entry is None or qty <= 0:
            body = Align.center(_side_text(None), vertical="middle")
            return Panel(
                body,
                title=Text("CURRENT POSITION", style=VALUE_STYLE),
                border_style=PANEL_STYLE,
                padding=PANEL_PADDING,
            )

        unrealized = (state.current_market_price - entry) * qty
        rows = Table.grid(expand=True)
        rows.add_column(ratio=1)
        rows.add_column(justify="right", ratio=1)

        side_line = Text()
        side_line.append("Side", style=LABEL_STYLE)
        side_value = _side_text(side) if side in {"UP", "DOWN"} else Text("UNKNOWN", style=MUTED_STYLE)

        ask_value = Text("NA", style=MUTED_STYLE)
        if state.position_ask is not None:
            ask_value = Text(f"{state.position_ask:.2f} USDC", style=f"bold {WARN_STYLE}")

        pnl_value = Text(_money(unrealized, signed=True), style=f"bold {_style_for_signed(unrealized)}")
        rows.add_row(side_line, side_value)
        rows.add_row(Text("Entry Price", style=LABEL_STYLE), Text(f"{entry:.2f} USDC", style=VALUE_STYLE))
        rows.add_row(Text("Target Ask", style=LABEL_STYLE), ask_value)
        rows.add_row(Text("Quantity", style=LABEL_STYLE), Text(f"{qty:.2f} shares", style=VALUE_STYLE))
        rows.add_row(Text("Mkt price", style=LABEL_STYLE), Text(f"{state.current_market_price:.2f} USDC", style=TEXT_STYLE))
        rows.add_row(Text("Unrealized PnL", style=LABEL_STYLE), pnl_value)

        return Panel(
            rows,
            title=Text("CURRENT POSITION", style=VALUE_STYLE),
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    def _account_summary_panel(self, state: DashboardState) -> Panel:
        pnl_style = f"bold {_style_for_signed(state.cumulative_pnl)}"
        visible_pnl_style = f"bold {_style_for_signed(state.visible_trades_pnl)}"

        pnl_grid = Table.grid(expand=True)
        pnl_grid.add_column(ratio=1)
        pnl_grid.add_column(justify="right", ratio=1)
        final = state.pnl_final_usdc if state.pnl_final_usdc is not None else state.cumulative_pnl
        pnl_grid.add_row(Text("Final PnL (=)", style=LABEL_STYLE), Text(_money(final, signed=True), style=pnl_style))
        if state.pnl_estimated_usdc is not None:
            est_style = f"{_style_for_signed(state.pnl_estimated_usdc)}" if state.pnl_estimated_usdc else MUTED_STYLE
            pnl_grid.add_row(Text("Estimated (~, bot TWAP)", style=LABEL_STYLE),
                             Text(_money(state.pnl_estimated_usdc, signed=True), style=est_style))
            pnl_grid.add_row(Text("Unresolved mkts (? / open / !)", style=LABEL_STYLE),
                             Text(str(state.pnl_unresolved_count), style=WARN_STYLE if state.pnl_unresolved_count else MUTED_STYLE))
        pnl_grid.add_row(Text("Visible final PnL", style=LABEL_STYLE), Text(_money(state.visible_trades_pnl, signed=True), style=visible_pnl_style))
        if state.pnl_journal_raw_usdc is not None:
            pnl_grid.add_row(Text("Journal raw cycle (legacy)", style=MUTED_STYLE),
                             Text(_money(state.pnl_journal_raw_usdc, signed=True), style=MUTED_STYLE))

        pnl_box = Panel(
            pnl_grid,
            title=Text(state.pnl_period_text or "", style=MUTED_STYLE) if state.pnl_period_text else None,
            title_align="left",
            border_style=PANEL_STYLE,
            padding=(0, 1),
        )
        usdc_box = Panel(
            Group(
                Text("USDC Balance", style=LABEL_STYLE),
                Text(_money(state.usdc_balance), style=VALUE_STYLE),
            ),
            border_style=PANEL_STYLE,
            padding=(0, 1),
        )
        pol_box = Panel(
            Group(
                Text("POL Balance", style=LABEL_STYLE),
                Text(f"{state.pol_balance:.4f} gas reserve", style=VALUE_STYLE),
            ),
            border_style=PANEL_STYLE,
            padding=(0, 1),
        )
        balances_grid = Table.grid(expand=True)
        balances_grid.add_column(ratio=1)
        balances_grid.add_column(ratio=1)
        balances_grid.add_row(usdc_box, pol_box)

        last_updated = state.account_last_updated.astimezone(timezone.utc).strftime("%H:%M UTC")
        return Panel(
            Group(pnl_box, balances_grid),
            title=Text("ACCOUNT SUMMARY", style=VALUE_STYLE),
            subtitle=Text(f"last updated: {last_updated}", style=MUTED_STYLE),
            subtitle_align="left",
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    def _recent_trades_panel(self, state: DashboardState) -> Panel:
        table = Table(
            expand=True,
            show_lines=False,
            show_edge=False,
            header_style=f"bold {LABEL_STYLE}",
            pad_edge=False,
        )
        table.add_column("#", style=MUTED_STYLE, justify="right", no_wrap=True)
        table.add_column("Market Slug", style=TEXT_STYLE, overflow="fold")
        table.add_column("Side", justify="center", no_wrap=True)
        table.add_column("Qty", justify="right", no_wrap=True)
        table.add_column("Entry", justify="right", no_wrap=True)
        table.add_column("Sold", justify="right", no_wrap=True)
        table.add_column("Position", justify="center", no_wrap=True)
        table.add_column("Outcome", justify="center", no_wrap=True)
        table.add_column("Redeem", justify="right", no_wrap=True)
        table.add_column("PnL", justify="right", no_wrap=True)

        if not state.trades:
            table.add_row(Text("—", style=MUTED_STYLE), Text("No trades yet", style=MUTED_STYLE), "", "", "", "", "", "", "", "")
        else:
            for idx, trade in enumerate(state.trades[:16], start=1):
                table.add_row(
                    Text(str(idx), style=MUTED_STYLE),
                    Text(trade.market_slug, style=LINK_STYLE),
                    _side_text(trade.side),
                    Text(f"{trade.qty:.2f}", style=TEXT_STYLE),
                    Text(_price(trade.entry_price), style=TEXT_STYLE),
                    self._sold_cell(trade),
                    self._position_cell(trade),
                    self._outcome_cell(trade),
                    self._redeem_cell(trade),
                    self._pnl_cell(trade),
                )

        return Panel(
            table,
            title=Text("RECENT TRADES", style=VALUE_STYLE),
            subtitle=Text("last 16 · PnL: = final  ~ estimated  ? pending  ! incomplete", style=MUTED_STYLE),
            subtitle_align="right",
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    def _bot_decision_panel(self, state: DashboardState) -> Panel:
        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(justify="right", ratio=1)
        table.add_column(width=2)
        table.add_column(ratio=1)
        table.add_column(justify="right", ratio=1)

        active_side = _side_text(state.active_side)
        if state.active_side is None:
            active_side = Text("NONE", style=MUTED_STYLE)
        book = self._book_text(state)
        robust = self._signed_optional_text(state.robust_net_usdc, money=False)
        p_fair = self._prob_text(state.p_fair)
        score = self._signed_optional_text(state.side_score, money=False)

        spacer = Text("")
        table.add_row(Text("Phase", style=LABEL_STYLE), Text(str(state.market_phase or "—"), style=TEXT_STYLE), spacer, Text("Active", style=LABEL_STYLE), active_side)
        table.add_row(Text("Time left", style=LABEL_STYLE), Text(_format_duration(_countdown_time_left(state)), style=TEXT_STYLE), spacer, Text("Side score", style=LABEL_STYLE), score)
        table.add_row(Text("p_fair", style=LABEL_STYLE), p_fair, spacer, Text("Book", style=LABEL_STYLE), book)
        table.add_row(Text("Robust net", style=LABEL_STYLE), robust, spacer, Text("Exposure", style=LABEL_STYLE), Text(_money(state.open_exposure_usdc), style=TEXT_STYLE))
        table.add_row(
            Text("Pending redeem", style=LABEL_STYLE),
            Text(f"{state.pending_redeem_count} / {_money(state.pending_redeem_usdc)}", style=WARN_STYLE if state.pending_redeem_count else MUTED_STYLE),
            spacer,
            Text("Last block", style=LABEL_STYLE),
            Text(_shorten(state.last_block_reason, 42), style=WARN_STYLE if state.last_block_reason else MUTED_STYLE),
        )

        return Panel(
            table,
            title=Text("BOT DECISION / ENTRY GATE", style=VALUE_STYLE),
            border_style=PANEL_STYLE,
            padding=PANEL_PADDING,
        )

    @staticmethod
    def _sold_cell(trade: TradeRecord) -> Text:
        if trade.sell_qty is None:
            return Text("—", style=MUTED_STYLE)
        if trade.sell_qty <= 0:
            return Text("0", style=MUTED_STYLE)
        return Text(f"{trade.sell_qty:.2f}", style=TEXT_STYLE)

    @staticmethod
    def _position_cell(trade: TradeRecord) -> Text:
        labels = {
            "FULLY_SOLD": ("sold", SOLD_STYLE), "PARTIALLY_SOLD_HELD": ("part+held", WARN_STYLE),
            "HELD": ("held", OPEN_STYLE), "NO_SHARES_HELD": ("none", MUTED_STYLE),
            "NO_TRACKED_ENTRY": ("ext?", NEG_STYLE),
        }
        text, style = labels.get(trade.position_state or "", ("—", MUTED_STYLE))
        return Text(text, style=style)

    @staticmethod
    def _outcome_cell(trade: TradeRecord) -> Text:
        labels = {
            "OFFICIAL": ("official", POS_STYLE), "ESTIMATED_TWAP": ("bot est", WARN_STYLE),
            "UNKNOWN": ("pending", WARN_STYLE), "NOT_ENDED": ("live", OPEN_STYLE),
        }
        text, style = labels.get(trade.outcome_state or "", ("—", MUTED_STYLE))
        return Text(text, style=style)

    @staticmethod
    def _redeem_cell(trade: TradeRecord) -> Text:
        state = trade.redeem_state or ""
        if state == "REDEEMED":
            return Text(_money(trade.redeem_amount or 0.0), style=VALUE_STYLE)
        if state == "CLAIMABLE":
            return Text(f"claim {_money(trade.expected_redeem_amount or 0.0)}", style=WARN_STYLE)
        if state == "NOTHING_TO_CLAIM":
            return Text(_money(0.0), style=MUTED_STYLE)
        if state == "AWAITING_OUTCOME":
            return Text("pending", style=WARN_STYLE)
        return Text("—", style=MUTED_STYLE)

    @staticmethod
    def _pnl_cell(trade: TradeRecord) -> Text:
        pnl = BTCDashboard._trade_pnl_amount(trade)
        mark = BASIS_MARK.get(trade.pnl_basis or "", "")
        if pnl is None:
            if trade.pnl_basis == "INCOMPLETE":
                return Text("! data", style=NEG_STYLE)
            if trade.pnl_basis == "PENDING":
                return Text("? pending", style=WARN_STYLE)
            return Text("—", style=MUTED_STYLE)
        style = f"bold {_style_for_signed(pnl)}" if mark == "=" else _style_for_signed(pnl)
        return Text(f"{mark}{_money(pnl, signed=True)}", style=style)

    @staticmethod
    def _trade_pnl_amount(trade: TradeRecord) -> Optional[float]:
        """Effective PnL only. UNKNOWN / missing payout is never turned into a total loss,
        and there is no exit-price x total-quantity fallback."""
        return trade.realized_pnl

    @staticmethod
    def _prob_text(value: Optional[float]) -> Text:
        if value is None:
            return Text("NA", style=MUTED_STYLE)
        style = POS_STYLE if value >= 0.55 else WARN_STYLE if value >= 0.47 else NEG_STYLE
        return Text(f"{value:.3f}", style=style)

    @staticmethod
    def _signed_optional_text(value: Optional[float], *, money: bool) -> Text:
        if value is None:
            return Text("NA", style=MUTED_STYLE)
        rendered = _money(value, signed=True) if money else f"{value:+.4f}"
        return Text(rendered, style=_style_for_signed(value))

    @staticmethod
    def _book_text(state: DashboardState) -> Text:
        if state.book_bid is None and state.book_ask is None and state.book_mid is None:
            return Text("NA", style=MUTED_STYLE)
        bid = "NA" if state.book_bid is None else f"{state.book_bid:.2f}"
        ask = "NA" if state.book_ask is None else f"{state.book_ask:.2f}"
        mid = "NA" if state.book_mid is None else f"{state.book_mid:.2f}"
        return Text(f"{bid}/{ask} m{mid}", style=TEXT_STYLE)


class TradeJournalDashboardSource:
    def __init__(self, db_path: Path, state: DashboardState, *, poll_sec: float = 1.0) -> None:
        self.db_path = db_path
        self.state = state
        self.poll_sec = max(0.5, float(poll_sec))
        self._effective_cache: Optional[tuple[float, dict]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="dashboard-db-source")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.refresh_once()
            self._stop_event.wait(self.poll_sec)

    def refresh_once(self) -> None:
        if not self.db_path.exists():
            return
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            try:
                self._refresh_from_conn(conn)
            finally:
                conn.close()
        except Exception:
            return

    def _refresh_from_conn(self, conn: sqlite3.Connection) -> None:
        market = self._latest_market_snapshot(conn)
        decision = self._latest_decision_snapshot(conn, str(market.get("slug") or ""))
        effective = self._effective_rows()
        summary = summarize_effective_pnl(effective)
        trades = self._recent_trades(conn, effective)
        visible_trades_pnl = self._visible_trades_pnl(trades)
        usdc_balance, pol_balance, account_updated = self._latest_account_snapshot(conn)
        pending_redeem_count, pending_redeem_usdc = self._pending_redeem_summary(effective)
        position_side, position_entry, position_qty, position_ask, current_market_price = self._current_position(
            conn,
            market_slug=str(market.get("slug") or ""),
            fallback_market_price=float(market.get("market_mid") or 0.0),
        )
        open_exposure_usdc = 0.0
        if position_entry is not None and position_qty is not None:
            open_exposure_usdc = float(position_entry) * float(position_qty)
        self.state.update(
            strike_price=float(market.get("strike") or 0.0),
            spot_price=float(market.get("spot") or 0.0),
            position_side=position_side,
            position_entry=position_entry,
            position_qty=position_qty,
            position_ask=position_ask,
            current_market_price=current_market_price,
            trades=trades,
            cumulative_pnl=float(summary["final_pnl_usdc"]),
            visible_trades_pnl=visible_trades_pnl,
            pnl_final_usdc=float(summary["final_pnl_usdc"]),
            pnl_estimated_usdc=float(summary["estimated_pnl_usdc"]),
            pnl_unresolved_count=int(summary["unresolved_count"]),
            pnl_period_text=self._period_text(summary),
            pnl_journal_raw_usdc=self._journal_raw_total(effective),
            usdc_balance=usdc_balance,
            pol_balance=pol_balance,
            account_last_updated=account_updated,
            market_phase=str(decision.get("phase") or "—"),
            active_side=decision.get("active_side"),
            time_left_sec=decision.get("time_left_sec"),
            decision_updated_at=decision.get("decision_updated_at"),
            side_score=decision.get("side_score"),
            p_fair=decision.get("p_fair"),
            book_bid=decision.get("book_bid"),
            book_ask=decision.get("book_ask"),
            book_mid=decision.get("book_mid"),
            robust_net_usdc=decision.get("robust_net_usdc"),
            last_block_reason=decision.get("last_block_reason"),
            pending_redeem_count=pending_redeem_count,
            pending_redeem_usdc=pending_redeem_usdc,
            open_exposure_usdc=open_exposure_usdc,
        )

    def _effective_rows(self) -> dict:
        """Shared projection (fills + journaled evidence), refreshed at most every 10 s."""
        now = time.time()
        if self._effective_cache is None or now - self._effective_cache[0] >= EFFECTIVE_PNL_REFRESH_SEC:
            self._effective_cache = (now, load_effective_market_pnl(self.db_path, as_of_ts=now))
        return self._effective_cache[1]

    @staticmethod
    def _period_text(summary: dict) -> str:
        first, last = summary.get("first_market_end_ts"), summary.get("last_market_end_ts")
        if not first or not last:
            return "no traded markets"
        fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d")  # noqa: E731
        return f"{fmt(first)}..{fmt(last)} UTC, {summary['market_count']} mkts"

    @staticmethod
    def _journal_raw_total(effective: dict) -> float:
        return float(sum(r["journal_cycle_pnl_usdc"] or 0.0 for r in effective.values()))

    @staticmethod
    def _visible_trades_pnl(trades: list[TradeRecord]) -> float:
        """Sum of the displayed rows' final PnL only (estimates and pending excluded)."""
        return float(sum(
            trade.realized_pnl for trade in trades[:16]
            if trade.realized_pnl is not None and BASIS_MARK.get(trade.pnl_basis or "") == "="
        ))

    @staticmethod
    def _latest_market_snapshot(conn: sqlite3.Connection) -> dict:
        row = conn.execute(
            """
            SELECT ts, payload_json
            FROM strategy_events
            WHERE event_type IN ('SIDE_DECISION', 'SIDE_DECISION_OBSERVATION', 'MAIN_SIGNAL_CANDIDATE_LIVE', 'LIVE_SIGNAL_COMPARE')
              AND payload_json LIKE '%"strike"%'
              AND payload_json LIKE '%"spot"%'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        payload = _json_loads(row["payload_json"] if row else None)
        return {
            "slug": payload.get("slug") or payload.get("market_slug") or "",
            "strike": payload.get("strike") or payload.get("market_open_spot") or 0.0,
            "spot": payload.get("spot") or payload.get("reference_spot_price") or 0.0,
            "market_mid": payload.get("market_mid") or 0.0,
            "ts": row["ts"] if row else None,
        }

    def _latest_decision_snapshot(self, conn: sqlite3.Connection, market_slug: str) -> dict:
        decision = self._latest_payload_for_slug(
            conn,
            [
                "ENTRY_CONFIRMATION_OBSERVATION",
                "LIVE_SIGNAL_COMPARE",
                "MAIN_SIGNAL_CANDIDATE_LIVE",
                "ENTRY_REGIME_OBSERVATION",
                "SIDE_DECISION",
                "SIDE_DECISION_OBSERVATION",
            ],
            market_slug,
        )
        block = self._latest_payload_for_slug(
            conn,
            [
                "BUY_PATH_DIAGNOSTIC",
                "NO_TRADE_ECON_GATE",
                "NO_TRADE_REDUCE_ONLY",
                "NO_TRADE_REDUCE_ONLY_TAIL_GUARD",
                "NO_TRADE_TREND_PROTECTION",
                "NO_TRADE_DIRECTIONAL_EDGE_GATE",
                "NO_TRADE_ACTIVE_SIDE_NONE",
            ],
            market_slug,
        )
        fair_payload = self._latest_payload_for_slug_with_any_field(
            conn,
            [
                "BUY_PATH_DIAGNOSTIC",
                "ENTRY_CONFIRMATION_OBSERVATION",
                "NO_TRADE_ECON_GATE",
                "NO_TRADE_REDUCE_ONLY",
                "NO_TRADE_REDUCE_ONLY_TAIL_GUARD",
                "NO_TRADE_TREND_PROTECTION",
                "NO_TRADE_DIRECTIONAL_EDGE_GATE",
            ],
            market_slug,
            ["p_fair", "fair", "fair_up", "fair_down"],
        )

        active_side = str(
            decision.get("active_side")
            or decision.get("main_active_side")
            or block.get("active_side")
            or decision.get("proposed_side")
            or ""
        ).upper()
        if active_side not in {"UP", "DOWN"}:
            active_side = None

        p_fair = decision.get("p_fair")
        if p_fair is None:
            fair_up = decision.get("fair_up")
            fair_down = decision.get("fair_down")
            if active_side == "UP":
                p_fair = fair_up
            elif active_side == "DOWN":
                p_fair = fair_down
            if p_fair is None and fair_up is not None and fair_down is not None:
                p_fair = max(float(fair_up), float(fair_down))
            if p_fair is None:
                p_fair = fair_payload.get("p_fair") or fair_payload.get("fair") or block.get("fair")

        book_bid = decision.get("best_bid") or decision.get("bid")
        book_ask = decision.get("best_ask") or decision.get("ask")
        if active_side == "UP":
            book_bid = book_bid if book_bid is not None else decision.get("bid_up")
            book_ask = book_ask if book_ask is not None else decision.get("ask_up")
        elif active_side == "DOWN":
            book_bid = book_bid if book_bid is not None else decision.get("bid_down")
            book_ask = book_ask if book_ask is not None else decision.get("ask_down")
        book_mid = decision.get("book_mid") or decision.get("market_mid")
        if book_bid is None:
            book_bid = block.get("bid")
        if book_ask is None:
            book_ask = block.get("ask")
        if book_mid is None and book_bid is not None and book_ask is not None:
            book_mid = (float(book_bid) + float(book_ask)) / 2.0

        decision_ts = decision.get("__ts")
        block_ts = block.get("__ts")
        time_left_source_ts = decision_ts if decision.get("time_left_sec") is not None else block_ts

        return {
            "phase": block.get("phase") or decision.get("phase") or "ACTIVE",
            "active_side": active_side,
            "time_left_sec": _optional_float(decision.get("time_left_sec") or block.get("time_left_sec")),
            "decision_updated_at": time_left_source_ts,
            "side_score": _optional_float(
                decision.get("side_score")
                or decision.get("composite_score")
                or decision.get("main_score")
                or block.get("side_score")
            ),
            "p_fair": _optional_float(p_fair),
            "book_bid": _optional_float(book_bid),
            "book_ask": _optional_float(book_ask),
            "book_mid": _optional_float(book_mid),
            "robust_net_usdc": _optional_float(
                decision.get("robust_net_usdc")
                or block.get("robust_net_usdc")
                or self._robust_from_reason(block.get("primary_reason") or block.get("blocked"))
            ),
            "last_block_reason": block.get("primary_reason") or block.get("blocked"),
        }

    @staticmethod
    def _latest_payload_for_slug(
        conn: sqlite3.Connection,
        event_types: list[str],
        market_slug: str,
    ) -> dict:
        placeholders = ",".join("?" for _ in event_types)
        params: list[str] = list(event_types)
        slug_clause = ""
        if market_slug:
            slug_clause = """
              AND (
                json_extract(payload_json, '$.slug') = ?
                OR json_extract(payload_json, '$.market_slug') = ?
              )
            """
            params.extend([market_slug, market_slug])
        row = conn.execute(
            f"""
            SELECT ts, payload_json
            FROM strategy_events
            WHERE event_type IN ({placeholders})
            {slug_clause}
            ORDER BY id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        payload = _json_loads(row["payload_json"] if row else None)
        if row:
            payload["__ts"] = _parse_dt(row["ts"])
        return payload

    @staticmethod
    def _latest_payload_for_slug_with_any_field(
        conn: sqlite3.Connection,
        event_types: list[str],
        market_slug: str,
        field_names: list[str],
    ) -> dict:
        placeholders = ",".join("?" for _ in event_types)
        field_clause = " OR ".join(f"json_extract(payload_json, '$.{name}') IS NOT NULL" for name in field_names)
        params: list[str] = list(event_types)
        slug_clause = ""
        if market_slug:
            slug_clause = """
              AND (
                json_extract(payload_json, '$.slug') = ?
                OR json_extract(payload_json, '$.market_slug') = ?
              )
            """
            params.extend([market_slug, market_slug])
        row = conn.execute(
            f"""
            SELECT ts, payload_json
            FROM strategy_events
            WHERE event_type IN ({placeholders})
              AND ({field_clause})
            {slug_clause}
            ORDER BY id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        payload = _json_loads(row["payload_json"] if row else None)
        if row:
            payload["__ts"] = _parse_dt(row["ts"])
        return payload

    @staticmethod
    def _robust_from_reason(reason: Optional[str]) -> Optional[float]:
        if not reason or "robust_net=" not in reason:
            return None
        raw = str(reason).split("robust_net=", 1)[1].split(" ", 1)[0]
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _latest_account_snapshot(conn: sqlite3.Connection) -> tuple[float, float, datetime]:
        row = conn.execute(
            """
            SELECT ts, payload_json
            FROM strategy_events
            WHERE event_type = 'ACCOUNT_SUMMARY'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return 0.0, 0.0, datetime.now(timezone.utc)
        payload = _json_loads(row["payload_json"])
        return (
            float(payload.get("usdc_balance") or 0.0),
            float(payload.get("pol_balance") or 0.0),
            _parse_dt(row["ts"]),
        )

    @staticmethod
    def _side_by_slug(conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute(
            """
            SELECT payload_json
            FROM strategy_events
            WHERE event_type IN (
              'MARKET_CYCLE_PNL',
              'MARKET_SETTLEMENT',
              'SIDE_DECISION',
              'POST_ENTRY_DECAY_REGISTERED',
              'STARTUP_INVENTORY_REHYDRATED'
            )
            ORDER BY id DESC
            LIMIT 2000
            """
        ).fetchall()
        sides: dict[str, str] = {}
        for row in rows:
            payload = _json_loads(row["payload_json"])
            slug = str(payload.get("slug") or payload.get("market_slug") or "")
            side = str(payload.get("inventory_side") or payload.get("entry_side") or payload.get("active_side") or "").upper()
            if slug and side in {"UP", "DOWN"} and slug not in sides:
                sides[slug] = side
        return sides

    def _recent_trades(self, conn: sqlite3.Connection, effective: dict) -> list[TradeRecord]:
        """Most recent traded markets from the shared effective-PnL projection."""
        side_by_slug = self._side_by_slug(conn)
        rows = sorted(
            (r for r in effective.values() if r["buy_fill_count"] or r["sell_fill_count"]),
            key=lambda r: str(r.get("last_fill_ts") or ""), reverse=True,
        )[:RECENT_TRADE_LIMIT]
        trades: list[TradeRecord] = []
        for idx, row in enumerate(rows, start=1):
            trades.append(TradeRecord(
                trade_id=idx,
                market_slug=row["slug"],
                side=row.get("entry_side") or side_by_slug.get(row["slug"], "—"),
                entry_price=float(row["entry_vwap"] or 0.0),
                qty=float(row["buy_qty"] or 0.0),
                exit_price=None,  # superseded by sell_qty / position_state (no exit x total-qty formula)
                redeem_amount=row["redeem_cash_usdc"],
                is_settled=row["outcome_state"] in ("OFFICIAL", "ESTIMATED_TWAP"),
                expected_redeem_amount=row["expected_payout_usdc"],
                realized_pnl=row["effective_pnl_usdc"],
                pnl_basis=row["pnl_basis"],
                position_state=row["position_state"],
                outcome_state=row["outcome_state"],
                redeem_state=row["redeem_state"],
                sell_qty=float(row["sell_qty"] or 0.0),
                held_shares=float(row["held_shares"] or 0.0),
                issues=tuple(row["issues"]),
            ))
        return trades

    @staticmethod
    def _settlement_by_slug(conn: sqlite3.Connection) -> dict[str, dict]:
        rows = conn.execute(
            """
            SELECT payload_json
            FROM strategy_events
            WHERE event_type = 'MARKET_SETTLEMENT'
            ORDER BY id DESC
            LIMIT 1000
            """
        ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            payload = _json_loads(row["payload_json"])
            slug = str(payload.get("slug") or payload.get("market_slug") or "")
            if slug and slug not in out:
                out[slug] = payload
        return out

    @staticmethod
    def _pending_redeem_summary(effective: dict) -> tuple[int, float]:
        """Claimable winning shares not yet redeemed (official or estimated outcome)."""
        claimable = [r for r in effective.values() if r["redeem_state"] == "CLAIMABLE"]
        return len(claimable), float(sum(r["expected_payout_usdc"] or 0.0 for r in claimable))

    def _current_position(
        self,
        conn: sqlite3.Connection,
        *,
        market_slug: str,
        fallback_market_price: float,
    ) -> tuple[Optional[str], Optional[float], Optional[float], Optional[float], float]:
        settlements = self._settlement_by_slug(conn)
        if not market_slug:
            return self._latest_open_position(conn, settlements=settlements, fallback_market_price=fallback_market_price)
        side_by_slug = self._side_by_slug(conn)
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN UPPER(side) = 'BUY' THEN qty ELSE -qty END) AS net_qty,
              SUM(CASE WHEN UPPER(side) = 'BUY' THEN price * qty ELSE 0 END) AS buy_cost,
              SUM(CASE WHEN UPPER(side) = 'BUY' THEN qty ELSE 0 END) AS buy_qty
            FROM order_events
            WHERE event_type = 'ORDER_FILLED'
              AND status = 'FILLED'
              AND json_extract(payload_json, '$.slug') = ?
            """,
            (market_slug,),
        ).fetchone()
        net_qty = float(row["net_qty"] or 0.0) if row else 0.0
        buy_qty = float(row["buy_qty"] or 0.0) if row else 0.0
        if net_qty <= 0 or buy_qty <= 0:
            return self._latest_open_position(
                conn,
                settlements=settlements,
                fallback_market_price=fallback_market_price,
                side_by_slug=side_by_slug,
                active_market_slug=market_slug,
            )
        entry = float(row["buy_cost"] or 0.0) / buy_qty
        ask = self._latest_active_sell_quote(conn, market_slug)
        market_price = fallback_market_price if fallback_market_price > 0 else entry
        return side_by_slug.get(market_slug), entry, net_qty, ask, market_price

    def _latest_open_position(
        self,
        conn: sqlite3.Connection,
        *,
        settlements: dict[str, dict],
        fallback_market_price: float,
        side_by_slug: Optional[dict[str, str]] = None,
        active_market_slug: str = "",
    ) -> tuple[Optional[str], Optional[float], Optional[float], Optional[float], float]:
        side_by_slug = side_by_slug or self._side_by_slug(conn)
        active_epoch = _slug_epoch(active_market_slug)
        rows = conn.execute(
            """
            SELECT
              json_extract(payload_json, '$.slug') AS slug,
              MAX(ts) AS last_ts,
              SUM(CASE WHEN UPPER(side) = 'BUY' THEN qty ELSE -qty END) AS net_qty,
              SUM(CASE WHEN UPPER(side) = 'BUY' THEN price * qty ELSE 0 END) AS buy_cost,
              SUM(CASE WHEN UPPER(side) = 'BUY' THEN qty ELSE 0 END) AS buy_qty
            FROM order_events
            WHERE event_type = 'ORDER_FILLED'
              AND status = 'FILLED'
              AND json_extract(payload_json, '$.slug') IS NOT NULL
            GROUP BY slug
            HAVING net_qty > 0
            ORDER BY last_ts DESC
            LIMIT 20
            """
        ).fetchall()
        for row in rows:
            slug = str(row["slug"] or "")
            if not slug or slug in settlements:
                continue
            slug_epoch = _slug_epoch(slug)
            if active_epoch is not None and slug_epoch is not None and slug_epoch not in {active_epoch, active_epoch - 900}:
                continue
            buy_qty = float(row["buy_qty"] or 0.0)
            net_qty = float(row["net_qty"] or 0.0)
            if buy_qty <= 0 or net_qty <= 0:
                continue
            entry = float(row["buy_cost"] or 0.0) / buy_qty
            ask = self._latest_active_sell_quote(conn, slug)
            market_price = fallback_market_price if fallback_market_price > 0 else entry
            return side_by_slug.get(slug), entry, net_qty, ask, market_price
        rehydrated = self._latest_rehydrated_position(
            conn,
            settlements=settlements,
            active_market_slug=active_market_slug,
        )
        if rehydrated is not None:
            slug, side, entry, qty = rehydrated
            ask = self._latest_active_sell_quote(conn, slug)
            market_price = fallback_market_price if fallback_market_price > 0 else entry
            return side, entry, qty, ask, market_price
        return None, None, None, None, fallback_market_price

    @staticmethod
    def _latest_rehydrated_position(
        conn: sqlite3.Connection,
        *,
        settlements: dict[str, dict],
        active_market_slug: str = "",
    ) -> Optional[tuple[str, Optional[str], float, float]]:
        active_epoch = _slug_epoch(active_market_slug)
        rows = conn.execute(
            """
            SELECT payload_json
            FROM strategy_events
            WHERE event_type = 'STARTUP_INVENTORY_REHYDRATED'
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
        for row in rows:
            payload = _json_loads(row["payload_json"])
            slug = str(payload.get("slug") or payload.get("market_slug") or "")
            if not slug or slug in settlements:
                continue
            slug_epoch = _slug_epoch(slug)
            if active_epoch is not None and slug_epoch is not None and slug_epoch not in {active_epoch, active_epoch - 900}:
                continue
            qty = _optional_float(payload.get("restored_total_qty")) or 0.0
            legs = payload.get("legs")
            entry = None
            if isinstance(legs, list) and legs:
                first_leg = legs[0] if isinstance(legs[0], dict) else {}
                entry = _optional_float(first_leg.get("avg_entry_price"))
            if qty <= 0 or entry is None:
                continue
            side = str(payload.get("inventory_side") or payload.get("active_side") or "").upper()
            if side not in {"UP", "DOWN"}:
                side = None
            return slug, side, entry, qty
        return None

    @staticmethod
    def _latest_active_sell_quote(conn: sqlite3.Connection, market_slug: str) -> Optional[float]:
        row = conn.execute(
            """
            SELECT price
            FROM order_events
            WHERE event_type = 'ORDER_SUBMIT'
              AND status = 'SUBMITTED'
              AND UPPER(side) = 'SELL'
              AND json_extract(payload_json, '$.slug') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (market_slug,),
        ).fetchone()
        return float(row["price"]) if row and row["price"] is not None else None


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
            TradeRecord(4, "btc-updown-15m-1791625500", "UP", 0.62, 8.5, None, None, False,
                        pnl_basis="OPEN", position_state="HELD", outcome_state="NOT_ENDED",
                        redeem_state="NOT_ENDED", sell_qty=0.0, held_shares=8.5),
            TradeRecord(3, "btc-updown-15m-1791624600", "DOWN", 0.71, 5.5, None, None, True, 5.5, 1.595,
                        pnl_basis="OUTCOME_CONFIRMED", position_state="HELD", outcome_state="OFFICIAL",
                        redeem_state="CLAIMABLE", sell_qty=0.0, held_shares=5.5),
            TradeRecord(2, "btc-updown-15m-1791623700", "UP", 0.80, 5.5, None, None, True, 0.0, -3.15,
                        pnl_basis="FILLS_FINAL", position_state="FULLY_SOLD", outcome_state="OFFICIAL",
                        redeem_state="NOT_REQUIRED", sell_qty=5.5, held_shares=0.0),
            TradeRecord(1, "btc-updown-15m-1791622800", "UP", 0.66, 10.0, None, 10.0, True, 10.0, 3.4,
                        pnl_basis="CASH_CONFIRMED", position_state="HELD", outcome_state="OFFICIAL",
                        redeem_state="REDEEMED", sell_qty=0.0, held_shares=10.0),
        ],
        cumulative_pnl=0.245,
        pnl_final_usdc=0.245,
        pnl_estimated_usdc=0.0,
        pnl_unresolved_count=1,
        pnl_period_text="mock",
        usdc_balance=142.60,
        pol_balance=2.341,
        account_last_updated=datetime.now(timezone.utc),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rich dashboard for the BTC 15-minute Polymarket bot.")
    parser.add_argument("--db", default=None, help="Path to trade_journal.db. Defaults to the canonical TRADE_DB_PATH (shell > .env > profile).")
    parser.add_argument("--mock", action="store_true", help="Use mock data instead of reading the trade DB.")
    args = parser.parse_args()

    state = _mock_state()
    source: Optional[TradeJournalDashboardSource] = None
    if not args.mock:
        db_path = _resolve_db_path(args.db)
        source = TradeJournalDashboardSource(db_path=db_path, state=state)
        source.refresh_once()

    dashboard = BTCDashboard(state)
    dashboard.start()
    if source is not None:
        source.start()
    try:
        tick = 0
        while True:
            time.sleep(1.0)
            if source is not None:
                continue
            tick += 1
            spot = 103612.34 + math.sin(tick / 4.0) * 32.0
            market_price = 0.68 + math.sin(tick / 5.0) * 0.03
            state.update(
                spot_price=spot,
                current_market_price=market_price,
                position_ask=round(min(0.99, market_price + 0.03), 2),
            )
    except KeyboardInterrupt:
        if source is not None:
            source.stop()
        dashboard.stop()
