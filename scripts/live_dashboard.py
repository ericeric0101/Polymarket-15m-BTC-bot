#!/usr/bin/env python3
"""
Simplified live trading dashboard.
Reads trade_journal.db and shows only essential trading metrics.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.wallet_ops import ensure_balance_clob_client, refresh_collateral_balance

LOCAL_TZ = timezone(timedelta(hours=8))
TZ_LABEL = "UTC+8"


def _to_local(iso_str: str) -> str:
    """Convert an ISO-format UTC timestamp string to local display string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str[:19]


def _time_ago(iso_str: str) -> str:
    """Return human-readable time-ago string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        return f"{mins // 60}h{mins % 60}m ago"
    except Exception:
        return "-"


load_dotenv(PROJECT_ROOT / ".env")


# ── Data ────────────────────────────────────────────────────────────────


@dataclass
class FillRecord:
    """One individual fill."""
    side: str           # "BUY" or "SELL"
    qty: float
    price: float
    notional: float     # price * qty
    liquidity: str      # "Maker" or "Taker"
    time_ago: str
    local_time: str     # HH:MM:SS in local tz


@dataclass
class TradingSnapshot:
    """All the metrics we care about."""

    # Counts
    buy_fills: int
    sell_fills: int
    total_fills: int

    # USDC values
    buy_cost_usdc: float
    sell_revenue_usdc: float
    fees_paid_usdc: float
    trade_pnl_usdc: float

    # Wallet
    wallet_error: str
    wallet_balance_usdc: Optional[float]

    # Win rate
    closed_positions: int
    winning_positions: int
    position_win_rate: float

    # Current state
    inventory_shares: float
    inventory_avg_entry: float

    # Latest
    last_fill_text: str
    last_fill_ago: str

    # Cycle stats
    cycle_total: int
    cycle_wins: int
    cycle_pnl_usdc: float

    # Recent fills for history panel
    recent_fills: List[FillRecord] = field(default_factory=list)


# ── DB Viewer ───────────────────────────────────────────────────────────


class DBViewer:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._balance_client: Any = None
        self._cached_balance: Optional[Decimal] = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def latest_strategy_start_ts(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ts FROM strategy_events WHERE event_type='STRATEGY_START' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["ts"] if row else None

    def fetch_wallet_balance(self) -> tuple[Optional[float], str]:
        """Returns (balance_or_none, error_message)."""
        try:
            self._balance_client = ensure_balance_clob_client(
                current_client=self._balance_client,
                logger_info_fn=lambda _: None,
                logger_warning_fn=lambda _: None,
            )
            if self._balance_client is None:
                return None, "需要 POLYMARKET_PK"
            self._balance_client, self._cached_balance = refresh_collateral_balance(
                current_client=self._balance_client,
                cached_balance=self._cached_balance,
                logger_info_fn=lambda _: None,
                logger_warning_fn=lambda _: None,
                logger_debug_fn=lambda _: None,
            )
            if self._cached_balance is not None:
                return float(self._cached_balance), ""
            return None, "API 無回應"
        except ImportError:
            return None, "需用 venv: ./venv/bin/python scripts/live_dashboard.py"
        except Exception as e:
            return None, str(e)[:40]

    def fetch_recent_fills(
        self, since_ts: Optional[str], limit: int = 20
    ) -> List[FillRecord]:
        """Fetch the most recent N fills from DB."""
        ts_filter = ""
        ts_params: tuple = ()
        if since_ts:
            ts_filter = "AND julianday(ts) >= julianday(?)"
            ts_params = (since_ts,)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT ts, side, price, qty, payload_json
                FROM order_events
                WHERE event_type='ORDER_FILLED' {ts_filter}
                ORDER BY id DESC LIMIT ?
                """,
                (*ts_params, limit),
            ).fetchall()

        fills: List[FillRecord] = []
        for r in rows:
            side_raw = str(r["side"]).lower()
            side = "BUY" if side_raw in ("1", "buy") else "SELL"
            price = float(r["price"] or 0)
            qty = float(r["qty"] or 0)

            payload = {}
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                pass
            liq_raw = str(payload.get("liquidity_side", ""))
            liquidity = "Maker" if liq_raw == "1" else "Taker" if liq_raw == "2" else "?"

            fills.append(FillRecord(
                side=side,
                qty=qty,
                price=price,
                notional=round(price * qty, 4),
                liquidity=liquidity,
                time_ago=_time_ago(r["ts"]),
                local_time=_to_local(r["ts"])[11:],  # HH:MM:SS only
            ))
        return fills

    def build_snapshot(
        self,
        since_ts: Optional[str],
        wallet_balance: Optional[float],
        wallet_error: str = "",
        recent_fills: Optional[List[FillRecord]] = None,
    ) -> TradingSnapshot:
        with self._connect() as conn:
            ts_filter = ""
            ts_params: tuple = ()
            if since_ts:
                ts_filter = "AND julianday(ts) >= julianday(?)"
                ts_params = (since_ts,)

            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_fills,
                    SUM(CASE WHEN lower(side) IN ('1','buy')  THEN 1 ELSE 0 END) AS buy_fills,
                    SUM(CASE WHEN lower(side) IN ('2','sell') THEN 1 ELSE 0 END) AS sell_fills,
                    COALESCE(SUM(CASE WHEN lower(side) IN ('1','buy')
                        THEN COALESCE(price,0) * COALESCE(qty,0) ELSE 0 END), 0) AS buy_cost,
                    COALESCE(SUM(CASE WHEN lower(side) IN ('2','sell')
                        THEN COALESCE(price,0) * COALESCE(qty,0) ELSE 0 END), 0) AS sell_revenue,
                    COALESCE(SUM(COALESCE(commission_usdc,0)), 0) AS fees
                FROM order_events
                WHERE event_type='ORDER_FILLED' {ts_filter}
                """,
                ts_params,
            ).fetchone()

            total_fills = int(row["total_fills"] or 0)
            buy_fills = int(row["buy_fills"] or 0)
            sell_fills = int(row["sell_fills"] or 0)
            buy_cost = float(row["buy_cost"] or 0.0)
            sell_revenue = float(row["sell_revenue"] or 0.0)
            fees = float(row["fees"] or 0.0)
            trade_pnl = sell_revenue - buy_cost - fees

            pos_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS closed,
                    SUM(CASE WHEN CAST(json_extract(payload_json,'$.realized_pnl') AS REAL) > 0
                        THEN 1 ELSE 0 END) AS wins
                FROM strategy_events
                WHERE event_type='POSITION_CLOSED' {ts_filter}
                """,
                ts_params,
            ).fetchone()

            closed = int(pos_row["closed"] or 0) if pos_row else 0
            wins = int(pos_row["wins"] or 0) if pos_row else 0
            win_rate = (wins / closed * 100.0) if closed > 0 else 0.0

            cyc_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS cycle_total,
                    SUM(CASE WHEN CAST(json_extract(payload_json,'$.cycle_combined_pnl_usdc') AS REAL) > 0
                        THEN 1 ELSE 0 END) AS cycle_wins,
                    COALESCE(SUM(CAST(json_extract(payload_json,'$.cycle_combined_pnl_usdc') AS REAL)), 0) AS cycle_pnl
                FROM strategy_events
                WHERE event_type='MARKET_CYCLE_PNL' {ts_filter}
                """,
                ts_params,
            ).fetchone()

            cycle_total = int(cyc_row["cycle_total"] or 0) if cyc_row else 0
            cycle_wins = int(cyc_row["cycle_wins"] or 0) if cyc_row else 0
            cycle_pnl = float(cyc_row["cycle_pnl"] or 0.0) if cyc_row else 0.0

            inv_row = conn.execute(
                f"""
                SELECT
                    json_extract(payload_json,'$.inventory_shares') AS shares,
                    json_extract(payload_json,'$.avg_entry_price') AS avg_entry
                FROM strategy_events
                WHERE event_type='MARKET_SETTLEMENT' {ts_filter}
                ORDER BY id DESC LIMIT 1
                """,
                ts_params,
            ).fetchone()

            inventory_shares = float(inv_row["shares"] or 0.0) if inv_row else 0.0
            inventory_avg_entry = float(inv_row["avg_entry"] or 0.0) if inv_row else 0.0

            last = conn.execute(
                f"""
                SELECT ts, side, price, qty
                FROM order_events
                WHERE event_type='ORDER_FILLED' {ts_filter}
                ORDER BY id DESC LIMIT 1
                """,
                ts_params,
            ).fetchone()

            if last:
                side_txt = "BUY" if str(last["side"]).lower() in ("1", "buy") else "SELL"
                last_fill_text = (
                    f"{side_txt}  {float(last['qty'] or 0):.3f} 份  "
                    f"@ {float(last['price'] or 0):.4f}"
                )
                last_fill_ago = _time_ago(last["ts"])
            else:
                last_fill_text = "無成交"
                last_fill_ago = "-"

        return TradingSnapshot(
            buy_fills=buy_fills,
            sell_fills=sell_fills,
            total_fills=total_fills,
            buy_cost_usdc=buy_cost,
            sell_revenue_usdc=sell_revenue,
            fees_paid_usdc=fees,
            trade_pnl_usdc=trade_pnl,
            wallet_error=wallet_error,
            closed_positions=closed,
            winning_positions=wins,
            position_win_rate=win_rate,
            wallet_balance_usdc=wallet_balance,
            inventory_shares=inventory_shares,
            inventory_avg_entry=inventory_avg_entry,
            last_fill_text=last_fill_text,
            last_fill_ago=last_fill_ago,
            cycle_total=cycle_total,
            cycle_wins=cycle_wins,
            cycle_pnl_usdc=cycle_pnl,
            recent_fills=recent_fills or [],
        )


# ── Layout ──────────────────────────────────────────────────────────────


def _color_pnl(value: float) -> Text:
    """Return colored text for profit/loss values."""
    txt = f"{value:+.4f}"
    if value > 0:
        return Text(txt, style="bold green")
    elif value < 0:
        return Text(txt, style="bold red")
    return Text(txt, style="dim")


def _pct_text(value: float) -> Text:
    txt = f"{value:.1f}%"
    if value >= 60:
        return Text(txt, style="bold green")
    elif value >= 40:
        return Text(txt, style="yellow")
    else:
        return Text(txt, style="bold red")


def _build_stats_panel(s: TradingSnapshot) -> Panel:
    """Left panel: summary stats."""
    table = Table(show_header=False, box=None, pad_edge=True, padding=(0, 2))
    table.add_column("label", style="bold cyan", width=16, no_wrap=True)
    table.add_column("value", style="white", min_width=22)

    # ── Wallet ──
    if s.wallet_balance_usdc is not None:
        bal = f"{s.wallet_balance_usdc:.4f} USDC"
    elif s.wallet_error:
        bal = f"⚠️  {s.wallet_error}"
    else:
        bal = "⏳ loading..."
    table.add_row("💰 錢包餘額", bal)
    table.add_row("", "")

    # ── Trade counts ──
    table.add_row("📈 買入筆數", str(s.buy_fills))
    table.add_row("📉 賣出筆數", str(s.sell_fills))
    table.add_row("📊 總成交", str(s.total_fills))
    table.add_row("", "")

    # ── Financials ──
    table.add_row("💵 買入總成本", f"{s.buy_cost_usdc:.4f} USDC")
    table.add_row("💵 賣出總收入", f"{s.sell_revenue_usdc:.4f} USDC")
    table.add_row("💸 手續費", f"{s.fees_paid_usdc:.4f} USDC")
    table.add_row("💰 交易損益", _color_pnl(s.trade_pnl_usdc))
    table.add_row("", "")

    # ── Win rate ──
    cycle_wr = (s.cycle_wins / s.cycle_total * 100.0) if s.cycle_total > 0 else 0.0
    table.add_row("🔄 Cycle 數", f"{s.cycle_total}  (勝 {s.cycle_wins})")
    table.add_row("🔄 Cycle 勝率", _pct_text(cycle_wr))
    table.add_row("🔄 Cycle PnL", _color_pnl(s.cycle_pnl_usdc))

    if s.closed_positions > 0:
        table.add_row("🏆 Position 勝率", _pct_text(s.position_win_rate))
        table.add_row("📋 已關閉 Position", f"{s.closed_positions}  (勝 {s.winning_positions})")
    table.add_row("", "")

    # ── Current state ──
    if s.inventory_shares > 0:
        table.add_row("📦 持倉數量", f"{s.inventory_shares:.4f} 份")
        if s.inventory_avg_entry > 0:
            table.add_row("📦 平均進場價", f"{s.inventory_avg_entry:.4f}")
    else:
        table.add_row("📦 持倉", "無")

    return Panel(table, title="[bold white]📊 Summary[/]", border_style="bright_blue", padding=(1, 1))


def _build_history_panel(fills: List[FillRecord]) -> Panel:
    """Right panel: recent trade history."""
    table = Table(
        show_header=True,
        header_style="bold white",
        box=None,
        pad_edge=True,
        padding=(0, 1),
    )
    table.add_column("方向", width=6, no_wrap=True)
    table.add_column("數量", width=8, justify="right")
    table.add_column("價格", width=8, justify="right")
    table.add_column("金額", width=10, justify="right")
    table.add_column("類型", width=7, no_wrap=True)
    table.add_column("時間", width=10, justify="right", style="dim")

    if not fills:
        table.add_row("", "", "尚無成交紀錄", "", "", "")
    else:
        for f in fills:
            side_text = Text(f.side, style="bold green" if f.side == "BUY" else "bold red")
            liq_text = Text(f.liquidity, style="cyan" if f.liquidity == "Maker" else "yellow")
            table.add_row(
                side_text,
                f"{f.qty:.3f}",
                f"{f.price:.4f}",
                f"${f.notional:.2f}",
                liq_text,
                f.time_ago,
            )

    return Panel(table, title="[bold white]📜 Trade History[/]", border_style="bright_cyan", padding=(1, 1))


def build_layout(s: TradingSnapshot, mode_label: str) -> Panel:
    """Top-level layout: stats left, history right using grid table."""
    stats = _build_stats_panel(s)
    history = _build_history_panel(s.recent_fills)

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(stats, history)

    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

    return Panel(
        grid,
        title=f"[bold white]BTC 15M Bot Dashboard[/]  [dim]({mode_label})[/]",
        subtitle=f"[dim]{now}  ({TZ_LABEL})[/dim]",
        border_style="bright_blue",
        padding=(0, 1),
    )


# ── Main ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 15M Bot – 簡易監控面板")
    parser.add_argument(
        "--db-path",
        default=str(PROJECT_ROOT / "logs" / "trade_journal.db"),
        help="Path to trade journal sqlite DB",
    )
    parser.add_argument(
        "--refresh-sec",
        type=float,
        default=3.0,
        help="Refresh interval (seconds)",
    )
    parser.add_argument(
        "--all-time",
        action="store_true",
        help="Show all-time stats instead of current session only",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=15,
        help="Number of recent trades to show in history panel (default: 15)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    viewer = DBViewer(db_path)

    since_ts: Optional[str] = None
    mode_label = "全部歷史"
    if not args.all_time:
        since_ts = viewer.latest_strategy_start_ts()
        if not since_ts:
            raise SystemExit("No STRATEGY_START found in DB. Use --all-time for all history.")
        mode_label = f"本次啟動: {_to_local(since_ts)}"

    console = Console()
    console.clear()

    wallet, wallet_err = viewer.fetch_wallet_balance()
    fills = viewer.fetch_recent_fills(since_ts, limit=args.history)
    snapshot = viewer.build_snapshot(since_ts, wallet, wallet_err, recent_fills=fills)

    with Live(
        build_layout(snapshot, mode_label),
        console=console,
        refresh_per_second=2,
        auto_refresh=False,
    ) as live:
        while True:
            if not args.all_time:
                new_ts = viewer.latest_strategy_start_ts()
                if new_ts and new_ts != since_ts:
                    since_ts = new_ts
                    mode_label = f"本次啟動: {_to_local(since_ts)}"

            wallet, wallet_err = viewer.fetch_wallet_balance()
            fills = viewer.fetch_recent_fills(since_ts, limit=args.history)
            snapshot = viewer.build_snapshot(since_ts, wallet, wallet_err, recent_fills=fills)
            live.update(build_layout(snapshot, mode_label), refresh=True)
            time.sleep(max(1.0, args.refresh_sec))


if __name__ == "__main__":
    main()
