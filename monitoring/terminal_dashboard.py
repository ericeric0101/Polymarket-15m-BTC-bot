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
            "current_buy_order": None,
            "current_sell_order": None,
            "redeem_runs": 0,
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

    def increment_redeem(self) -> None:
        with self._lock:
            self._state["redeem_runs"] += 1
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

        # Left panel: Orders and Slug
        left_grid = Table.grid(expand=True, padding=(0, 0))
        left_grid.add_column()
        
        buy_text = snapshot.get("current_buy_order") or "None"
        sell_text = snapshot.get("current_sell_order") or ""
        slug = str(snapshot["slug"])
        
        left_grid.add_row(Panel(buy_text, title="Current Buy Order", border_style="cyan"))
        if sell_text:
            left_grid.add_row(Panel(sell_text, title="Current Sell Order", border_style="magenta"))
        strike_val = snapshot.get("strike")
        spot_val = snapshot.get("spot")
        strike_str = f"${strike_val:,.2f}" if strike_val else "..."
        spot_str = f"${spot_val:,.2f}" if spot_val else "..."
        market_text = f"{slug}\nStrike: [bold]{strike_str}[/bold] | Spot: [bold]{spot_str}[/bold]"
        
        left_grid.add_row(Panel(market_text, title="Market", border_style="blue"))

        # Right panel: Stats
        stats_table = Table(show_header=False, box=None, pad_edge=False)
        stats_table.add_column("k", style="bold green", width=16)
        stats_table.add_column("v", style="white")
        stats_table.add_row("Inventory", f"{float(snapshot.get('inventory_shares', 0.0)):.4f}")
        stats_table.add_row("Total Trades", str(snapshot["fills_total"]))
        stats_table.add_row("Buys", str(snapshot["maker_buy_fills"]))
        stats_table.add_row("Sells", str(int(snapshot.get("maker_sell_fills", 0)) + int(snapshot.get("taker_exit_fills", 0))))
        stats_table.add_row("Redeems", str(snapshot.get("redeem_runs", 0)))
        stats_table.add_row("Live PnL", f"{float(snapshot['cycle_pnl_usdc']):+.4f} USDC")
        wallet_balance = snapshot["wallet_balance_usdc"]
        stats_table.add_row("Wallet", "..." if wallet_balance is None else f"{float(wallet_balance):.4f} USDC")

        # Split layout
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(
            Panel(left_grid, title="Orders", border_style="cyan"),
            Panel(stats_table, title="Stats", border_style="green")
        )

        return Panel(grid, title=f"{self.title} Live", border_style="yellow")

    def _run(self) -> None:
        with Live(
            self._build_layout(),
            refresh_per_second=max(1, int(round(1.0 / self.refresh_interval_sec))),
            transient=False,
            auto_refresh=False,
        ) as live:
            while not self._stop_event.wait(self.refresh_interval_sec):
                live.update(self._build_layout(), refresh=True)
