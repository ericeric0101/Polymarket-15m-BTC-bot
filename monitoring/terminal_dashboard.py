import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


class TerminalDashboard:
    """Minimal Rich terminal dashboard for live trading session stats."""

    def __init__(
        self,
        title: str = "BTC 15M Bot",
        refresh_interval_sec: float = 1.0,
    ) -> None:
        self.title = title
        self.refresh_interval_sec = max(0.2, float(refresh_interval_sec))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "started_at": datetime.now(timezone.utc),
            "phase": "WAITING",
            "slug": "-",
            "active_side": "NONE",
            "inventory_shares": 0.0,
            "wallet_balance_usdc": None,
            "fills_total": 0,
            "maker_fills": 0,
            "taker_fills": 0,
            "maker_buy_fills": 0,
            "maker_sell_fills": 0,
            "taker_exit_fills": 0,
            "fees_paid_usdc": 0.0,
            "cycle_total": 0,
            "cycle_wins": 0,
            "cycle_pnl_usdc": 0.0,
            "round_trips_closed": 0,
            "position_win_rate": 0.0,
            "position_realized_pnl": 0.0,
            "active_orders": 0,
            "last_fill": "-",
            "last_cycle": "-",
            "last_update": datetime.now(timezone.utc),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="terminal-dashboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)
            self._state["last_update"] = datetime.now(timezone.utc)

    def increment_fill(
        self,
        *,
        is_maker_fill: bool,
        side: str,
        qty: float,
        price: float,
        commission_usdc: float,
        client_order_id: str,
        is_taker_exit: bool,
    ) -> None:
        with self._lock:
            self._state["fills_total"] += 1
            self._state["fees_paid_usdc"] += float(commission_usdc or 0.0)
            if is_maker_fill:
                self._state["maker_fills"] += 1
                if side == "buy":
                    self._state["maker_buy_fills"] += 1
                elif side == "sell":
                    self._state["maker_sell_fills"] += 1
            else:
                self._state["taker_fills"] += 1
                if is_taker_exit:
                    self._state["taker_exit_fills"] += 1
            self._state["last_fill"] = (
                f"{client_order_id} {side.upper()} {qty:.3f} @ {price:.4f} "
                f"{'MAKER' if is_maker_fill else 'TAKER'}"
            )
            self._state["last_update"] = datetime.now(timezone.utc)

    def record_cycle(self, *, slug: str, pnl_usdc: float) -> None:
        with self._lock:
            self._state["cycle_total"] += 1
            if pnl_usdc > 0:
                self._state["cycle_wins"] += 1
            self._state["cycle_pnl_usdc"] += float(pnl_usdc)
            self._state["last_cycle"] = f"{slug} pnl={pnl_usdc:+.4f}"
            self._state["last_update"] = datetime.now(timezone.utc)

    def record_position_closed(self, *, realized_pnl: float, total_trades: int, win_rate: float) -> None:
        with self._lock:
            self._state["round_trips_closed"] = int(total_trades)
            self._state["position_win_rate"] = float(win_rate)
            self._state["position_realized_pnl"] = float(self._state["position_realized_pnl"]) + float(realized_pnl)
            self._state["last_update"] = datetime.now(timezone.utc)

    def _build_layout(self) -> Group:
        with self._lock:
            snapshot = dict(self._state)

        session_table = Table(show_header=False, box=None, pad_edge=False)
        session_table.add_column("k", style="bold cyan", width=14)
        session_table.add_column("v", style="white")
        session_table.add_row("Phase", str(snapshot["phase"]))
        session_table.add_row("Slug", str(snapshot["slug"]))
        session_table.add_row("Active Side", str(snapshot["active_side"]))
        session_table.add_row("Inventory", f"{float(snapshot['inventory_shares']):.4f}")
        wallet_balance = snapshot["wallet_balance_usdc"]
        session_table.add_row(
            "USDC.e",
            "-" if wallet_balance is None else f"{float(wallet_balance):.4f}",
        )
        session_table.add_row("Active Orders", str(snapshot["active_orders"]))

        stats_table = Table(show_header=False, box=None, pad_edge=False)
        stats_table.add_column("k", style="bold green", width=18)
        stats_table.add_column("v", style="white")
        stats_table.add_row("Fills", str(snapshot["fills_total"]))
        stats_table.add_row("Maker Fills", str(snapshot["maker_fills"]))
        stats_table.add_row("Taker Fills", str(snapshot["taker_fills"]))
        stats_table.add_row("Maker Buy", str(snapshot["maker_buy_fills"]))
        stats_table.add_row("Maker Sell", str(snapshot["maker_sell_fills"]))
        stats_table.add_row("Taker Exit", str(snapshot["taker_exit_fills"]))
        stats_table.add_row("Fees Paid", f"{float(snapshot['fees_paid_usdc']):.4f}")
        cycle_total = int(snapshot["cycle_total"])
        cycle_wins = int(snapshot["cycle_wins"])
        cycle_win_rate = (cycle_wins / cycle_total * 100.0) if cycle_total > 0 else 0.0
        stats_table.add_row("Cycle Win Rate", f"{cycle_win_rate:.1f}%")
        stats_table.add_row("Cycle PnL", f"{float(snapshot['cycle_pnl_usdc']):+.4f}")
        stats_table.add_row("Closed Trades", str(snapshot["round_trips_closed"]))
        stats_table.add_row("Trade Win Rate", f"{float(snapshot['position_win_rate']):.1f}%")

        latest_table = Table(show_header=False, box=None, pad_edge=False)
        latest_table.add_column("k", style="bold yellow", width=12)
        latest_table.add_column("v", style="white", overflow="fold")
        latest_table.add_row("Last Fill", str(snapshot["last_fill"]))
        latest_table.add_row("Last Cycle", str(snapshot["last_cycle"]))
        latest_table.add_row(
            "Updated",
            snapshot["last_update"].astimezone().strftime("%H:%M:%S"),
        )
        latest_table.add_row(
            "Started",
            snapshot["started_at"].astimezone().strftime("%H:%M:%S"),
        )

        return Group(
            Panel(session_table, title=f"{self.title} Session", border_style="cyan"),
            Panel(stats_table, title="Trading Stats", border_style="green"),
            Panel(latest_table, title="Latest", border_style="yellow"),
        )

    def _run(self) -> None:
        with Live(
            self._build_layout(),
            refresh_per_second=max(1, int(round(1.0 / self.refresh_interval_sec))),
            transient=False,
            auto_refresh=False,
        ) as live:
            while not self._stop_event.wait(self.refresh_interval_sec):
                live.update(self._build_layout(), refresh=True)
