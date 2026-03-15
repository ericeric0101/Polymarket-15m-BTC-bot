#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bot.wallet_ops import ensure_balance_clob_client, refresh_collateral_balance


load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class DashboardSnapshot:
    started_at: Optional[str]
    refresh_sec: float
    phase: str
    slug: str
    active_side: str
    inventory_shares: float
    fills_total: int
    maker_fills: int
    taker_fills: int
    maker_buy_fills: int
    maker_sell_fills: int
    taker_exit_fills: int
    fees_paid_usdc: float
    cycle_total: int
    cycle_wins: int
    cycle_pnl_usdc: float
    wallet_balance_usdc: Optional[float]
    wallet_balance_pol: Optional[float]
    last_fill: str
    last_cycle: str


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
                "select ts from strategy_events where event_type='STRATEGY_START' order by id desc limit 1"
            ).fetchone()
            return row["ts"] if row else None

    def fetch_wallet_balance(self) -> Optional[float]:
        self._balance_client = ensure_balance_clob_client(
            current_client=self._balance_client,
            logger_info_fn=lambda _msg: None,
            logger_warning_fn=lambda _msg: None,
        )
        self._balance_client, self._cached_balance = refresh_collateral_balance(
            current_client=self._balance_client,
            cached_balance=self._cached_balance,
            logger_info_fn=lambda _msg: None,
            logger_warning_fn=lambda _msg: None,
            logger_debug_fn=lambda _msg: None,
        )
        return float(self._cached_balance) if self._cached_balance is not None else None

    def fetch_pol_balance(self) -> Optional[float]:
        try:
            from web3 import Web3

            wallet = (os.getenv("POLYMARKET_WALLET_ADDRESS") or "").strip()
            rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com").strip()
            if not wallet or not rpc_url:
                return None

            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
            if not w3.is_connected():
                return None
            wei_balance = w3.eth.get_balance(w3.to_checksum_address(wallet))
            return float(w3.from_wei(wei_balance, "ether"))
        except Exception:
            return None

    def build_snapshot(
        self,
        started_at: str,
        wallet_balance_usdc: Optional[float],
        wallet_balance_pol: Optional[float],
        refresh_sec: float,
    ) -> DashboardSnapshot:
        with self._connect() as conn:
            latest_phase = conn.execute(
                """
                select json_extract(payload_json,'$.to') as phase,
                       json_extract(payload_json,'$.slug') as slug
                from strategy_events
                where julianday(ts) >= julianday(?) and event_type='MARKET_PHASE_CHANGE'
                order by id desc limit 1
                """,
                (started_at,),
            ).fetchone()
            latest_side = conn.execute(
                """
                select json_extract(payload_json,'$.active_side') as active_side,
                       json_extract(payload_json,'$.slug') as slug
                from strategy_events
                where julianday(ts) >= julianday(?)
                  and event_type in ('SIDE_MODE_CHANGED','SIDE_MODE_FLIPPED')
                order by id desc limit 1
                """,
                (started_at,),
            ).fetchone()
            latest_fill = conn.execute(
                """
                select client_order_id, side, price, qty,
                       json_extract(payload_json,'$.liquidity_side') as liquidity_side
                from order_events
                where julianday(ts) >= julianday(?) and event_type='ORDER_FILLED'
                order by id desc limit 1
                """,
                (started_at,),
            ).fetchone()
            latest_cycle = conn.execute(
                """
                select json_extract(payload_json,'$.slug') as slug,
                       json_extract(payload_json,'$.cycle_combined_pnl_usdc') as pnl
                from strategy_events
                where julianday(ts) >= julianday(?) and event_type='MARKET_CYCLE_PNL'
                order by id desc limit 1
                """,
                (started_at,),
            ).fetchone()
            inventory_row = conn.execute(
                """
                select json_extract(payload_json,'$.inventory_shares') as inventory_shares
                from strategy_events
                where julianday(ts) >= julianday(?) and event_type='MARKET_SETTLEMENT'
                order by id desc limit 1
                """,
                (started_at,),
            ).fetchone()

            stats = conn.execute(
                """
                with fills as (
                    select
                        client_order_id,
                        side,
                        commission_usdc,
                        json_extract(payload_json,'$.liquidity_side') as liquidity_side
                    from order_events
                    where julianday(ts) >= julianday(?)
                      and event_type='ORDER_FILLED'
                ),
                cycles as (
                    select
                        json_extract(payload_json,'$.cycle_combined_pnl_usdc') as pnl
                    from strategy_events
                    where julianday(ts) >= julianday(?)
                      and event_type='MARKET_CYCLE_PNL'
                )
                select
                    (select count(*) from fills) as fills_total,
                    (select count(*) from fills where liquidity_side='1') as maker_fills,
                    (select count(*) from fills where liquidity_side='2') as taker_fills,
                    (select count(*) from fills where liquidity_side='1' and lower(side) in ('1','buy')) as maker_buy_fills,
                    (select count(*) from fills where liquidity_side='1' and lower(side) in ('2','sell')) as maker_sell_fills,
                    (select count(*) from fills where client_order_id like 'BTC-15M-TAKER-EXIT-%') as taker_exit_fills,
                    (select coalesce(sum(coalesce(commission_usdc,0)),0) from fills) as fees_paid_usdc,
                    (select count(*) from cycles) as cycle_total,
                    (select count(*) from cycles where pnl > 0) as cycle_wins,
                    (select coalesce(sum(coalesce(pnl,0)),0) from cycles) as cycle_pnl_usdc
                """,
                (started_at, started_at),
            ).fetchone()

        fill_text = "-"
        if latest_fill:
            liq = "MAKER" if str(latest_fill["liquidity_side"]) == "1" else "TAKER"
            fill_text = (
                f"{latest_fill['client_order_id']} "
                f"{str(latest_fill['side']).upper()} {float(latest_fill['qty'] or 0):.3f} "
                f"@ {float(latest_fill['price'] or 0):.4f} {liq}"
            )

        cycle_text = "-"
        if latest_cycle:
            cycle_text = (
                f"{latest_cycle['slug']} pnl={float(latest_cycle['pnl'] or 0):+.4f}"
            )

        phase = latest_phase["phase"] if latest_phase and latest_phase["phase"] else "UNKNOWN"
        slug = "-"
        if latest_phase and latest_phase["slug"]:
            slug = latest_phase["slug"]
        elif latest_side and latest_side["slug"]:
            slug = latest_side["slug"]

        active_side = latest_side["active_side"] if latest_side and latest_side["active_side"] else "NONE"

        return DashboardSnapshot(
            started_at=started_at,
            refresh_sec=refresh_sec,
            phase=str(phase),
            slug=str(slug),
            active_side=str(active_side),
            inventory_shares=float(inventory_row["inventory_shares"] or 0.0) if inventory_row else 0.0,
            fills_total=int(stats["fills_total"] or 0),
            maker_fills=int(stats["maker_fills"] or 0),
            taker_fills=int(stats["taker_fills"] or 0),
            maker_buy_fills=int(stats["maker_buy_fills"] or 0),
            maker_sell_fills=int(stats["maker_sell_fills"] or 0),
            taker_exit_fills=int(stats["taker_exit_fills"] or 0),
            fees_paid_usdc=float(stats["fees_paid_usdc"] or 0.0),
            cycle_total=int(stats["cycle_total"] or 0),
            cycle_wins=int(stats["cycle_wins"] or 0),
            cycle_pnl_usdc=float(stats["cycle_pnl_usdc"] or 0.0),
            wallet_balance_usdc=wallet_balance_usdc,
            wallet_balance_pol=wallet_balance_pol,
            last_fill=fill_text,
            last_cycle=cycle_text,
        )


def build_layout(snapshot: DashboardSnapshot) -> Group:
    session_table = Table(show_header=False, box=None, pad_edge=False)
    session_table.add_column("k", style="bold cyan", width=14)
    session_table.add_column("v", style="white")
    session_table.add_row("Since", snapshot.started_at or "-")
    session_table.add_row("Phase", snapshot.phase)
    session_table.add_row("Slug", snapshot.slug)
    session_table.add_row("Active Side", snapshot.active_side)
    session_table.add_row("Inventory", f"{snapshot.inventory_shares:.4f}")
    session_table.add_row(
        "USDC.e",
        "-" if snapshot.wallet_balance_usdc is None else f"{snapshot.wallet_balance_usdc:.4f}",
    )
    session_table.add_row(
        "POL",
        "-" if snapshot.wallet_balance_pol is None else f"{snapshot.wallet_balance_pol:.4f}",
    )
    session_table.add_row("Refresh", f"{snapshot.refresh_sec:.1f}s")

    stats_table = Table(show_header=False, box=None, pad_edge=False)
    stats_table.add_column("k", style="bold green", width=18)
    stats_table.add_column("v", style="white")
    stats_table.add_row("Fills", str(snapshot.fills_total))
    stats_table.add_row("Maker Fills", str(snapshot.maker_fills))
    stats_table.add_row("Taker Fills", str(snapshot.taker_fills))
    stats_table.add_row("Maker Buy", str(snapshot.maker_buy_fills))
    stats_table.add_row("Maker Sell", str(snapshot.maker_sell_fills))
    stats_table.add_row("Taker Exit", str(snapshot.taker_exit_fills))
    stats_table.add_row("Fees Paid", f"{snapshot.fees_paid_usdc:.4f}")
    cycle_win_rate = (snapshot.cycle_wins / snapshot.cycle_total * 100.0) if snapshot.cycle_total else 0.0
    stats_table.add_row("Cycles Ended", str(snapshot.cycle_total))
    stats_table.add_row("Cycle Win Rate", f"{cycle_win_rate:.1f}%")
    stats_table.add_row("Cycle PnL", f"{snapshot.cycle_pnl_usdc:+.4f}")

    latest_table = Table(show_header=False, box=None, pad_edge=False)
    latest_table.add_column("k", style="bold yellow", width=12)
    latest_table.add_column("v", style="white", overflow="fold")
    latest_table.add_row("Last Fill", snapshot.last_fill)
    latest_table.add_row("Last Cycle", snapshot.last_cycle)
    if snapshot.cycle_total == 0:
        latest_table.add_row("Cycle Note", "No completed cycle after current STRATEGY_START yet.")
    latest_table.add_row("Now", datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"))

    return Group(
        Panel(session_table, title="Session", border_style="cyan"),
        Panel(stats_table, title="Trading Stats", border_style="green"),
        Panel(latest_table, title="Latest", border_style="yellow"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Rich terminal dashboard for run_bot")
    parser.add_argument(
        "--db-path",
        default=str(PROJECT_ROOT / "logs" / "trade_journal.db"),
        help="Path to trade journal sqlite DB",
    )
    parser.add_argument(
        "--refresh-sec",
        type=float,
        default=2.0,
        help="Dashboard refresh interval in seconds",
    )
    parser.add_argument(
        "--since",
        choices=("latest-start",),
        default="latest-start",
        help="Stats window anchor",
    )
    args = parser.parse_args()

    viewer = DBViewer(Path(args.db_path))
    start_ts = viewer.latest_strategy_start_ts()
    if not start_ts:
        raise SystemExit("No STRATEGY_START found in DB.")

    with Live(
        build_layout(
            viewer.build_snapshot(
                start_ts,
                viewer.fetch_wallet_balance(),
                viewer.fetch_pol_balance(),
                args.refresh_sec,
            )
        ),
        refresh_per_second=4,
        auto_refresh=False,
    ) as live:
        while True:
            start_ts = viewer.latest_strategy_start_ts() or start_ts
            wallet_balance = viewer.fetch_wallet_balance()
            pol_balance = viewer.fetch_pol_balance()
            snapshot = viewer.build_snapshot(start_ts, wallet_balance, pol_balance, args.refresh_sec)
            live.update(build_layout(snapshot), refresh=True)
            time.sleep(max(0.5, args.refresh_sec))


if __name__ == "__main__":
    main()
