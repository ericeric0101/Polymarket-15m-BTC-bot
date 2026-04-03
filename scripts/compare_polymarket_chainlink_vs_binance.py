from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from loguru import logger

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.price_streams import (
    BINANCE_AGGTRADE_WS_URL,
    POLYMARKET_CHAINLINK_SUBSCRIBE_PAYLOAD,
    POLYMARKET_LIVE_WS_URL,
    PriceTick,
    extract_binance_aggtrade_tick,
    extract_polymarket_chainlink_tick,
)


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.binance_tick: Optional[PriceTick] = None
        self.polymarket_tick: Optional[PriceTick] = None
        self.polymarket_raw_preview: Optional[str] = None

    def set_tick(self, *, name: str, tick: PriceTick) -> None:
        with self.lock:
            if name == "binance":
                self.binance_tick = tick
            elif name == "polymarket":
                self.polymarket_tick = tick

    def set_polymarket_preview(self, text: str) -> None:
        with self.lock:
            self.polymarket_raw_preview = text

    def snapshot(self) -> tuple[Optional[PriceTick], Optional[PriceTick], Optional[str]]:
        with self.lock:
            return self.binance_tick, self.polymarket_tick, self.polymarket_raw_preview


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not dec.is_finite():
        return None
    return dec


def _run_binance_stream(state: SharedState, stop_event: threading.Event) -> None:
    import websockets.sync.client as ws_sync  # type: ignore

    reconnect_delay = 1.0
    while not stop_event.is_set():
        try:
            with ws_sync.connect(
                BINANCE_AGGTRADE_WS_URL,
                close_timeout=5,
                ping_interval=None,
                ping_timeout=None,
            ) as ws:
                reconnect_delay = 1.0
                logger.info("Binance WS connected")
                while not stop_event.is_set():
                    raw = ws.recv(timeout=5)
                    tick = extract_binance_aggtrade_tick(raw)
                    if tick is None:
                        continue
                    state.set_tick(name="binance", tick=tick)
        except TimeoutError:
            continue
        except Exception as exc:
            logger.debug(f"Binance WS error: {exc!r}; reconnect in {reconnect_delay:.1f}s")
            stop_event.wait(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2.0, 10.0)


def _run_polymarket_stream(
    state: SharedState,
    stop_event: threading.Event,
    *,
    dump_raw: bool,
) -> None:
    import websockets.sync.client as ws_sync  # type: ignore

    reconnect_delay = 1.0
    while not stop_event.is_set():
        try:
            with ws_sync.connect(
                POLYMARKET_LIVE_WS_URL,
                close_timeout=5,
                ping_interval=None,
                ping_timeout=None,
            ) as ws:
                reconnect_delay = 1.0
                logger.info("Polymarket live WS connected")
                ws.send(json.dumps(POLYMARKET_CHAINLINK_SUBSCRIBE_PAYLOAD))
                while not stop_event.is_set():
                    raw = ws.recv(timeout=5)
                    state.set_polymarket_preview(str(raw)[:600])
                    if dump_raw:
                        logger.info(f"Polymarket raw: {str(raw)[:600]}")
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    tick = extract_polymarket_chainlink_tick(payload)
                    if tick is None:
                        continue
                    state.set_tick(name="polymarket", tick=tick)
        except TimeoutError:
            continue
        except Exception as exc:
            logger.debug(f"Polymarket WS error: {exc!r}; reconnect in {reconnect_delay:.1f}s")
            stop_event.wait(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2.0, 10.0)


def _fmt_age(tick: Optional[PriceTick]) -> str:
    if tick is None:
        return "-"
    return f"{max(0.0, time.time() - tick.received_at_ts):5.1f}s"


def _fmt_ts_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))


def _print_status(state: SharedState) -> None:
    binance_tick, polymarket_tick, preview = state.snapshot()
    binance_px = binance_tick.price if binance_tick else None
    polymarket_px = polymarket_tick.price if polymarket_tick else None
    delta_abs = None
    delta_bps = None
    if binance_px is not None and polymarket_px is not None and binance_px > 0:
        delta_abs = polymarket_px - binance_px
        delta_bps = (delta_abs / binance_px) * Decimal("10000")

    print(
        "binance="
        f"{f'{float(binance_px):,.2f}' if binance_px is not None else '-':>12} "
        f"(age={_fmt_age(binance_tick)}, ts={_fmt_ts_ms(binance_tick.updated_at_ms if binance_tick else None)}) | "
        "polymarket="
        f"{f'{float(polymarket_px):,.2f}' if polymarket_px is not None else '-':>12} "
        f"(age={_fmt_age(polymarket_tick)}, ts={_fmt_ts_ms(polymarket_tick.updated_at_ms if polymarket_tick else None)}) | "
        "delta="
        f"{(f'{float(delta_abs):+.2f}' if delta_abs is not None else '-'):>10} "
        f"{(f'({float(delta_bps):+.2f} bps)' if delta_bps is not None else '')}"
    )
    if polymarket_tick is None and preview:
        print(f"  polymarket preview: {preview[:240]}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Compare Binance BTC price vs Polymarket live Chainlink BTC price."
    )
    ap.add_argument(
        "--duration-sec",
        type=int,
        default=0,
        help="Stop automatically after N seconds. 0 means run until Ctrl+C.",
    )
    ap.add_argument(
        "--print-interval-sec",
        type=float,
        default=1.0,
        help="Status print interval.",
    )
    ap.add_argument(
        "--dump-polymarket-raw",
        action="store_true",
        help="Print raw Polymarket WS messages for parser validation.",
    )
    return ap


def main() -> int:
    args = build_parser().parse_args()
    state = SharedState()
    stop_event = threading.Event()

    threads = [
        threading.Thread(
            target=_run_binance_stream,
            args=(state, stop_event),
            name="binance-price-compare",
            daemon=True,
        ),
        threading.Thread(
            target=_run_polymarket_stream,
            args=(state, stop_event),
            kwargs={"dump_raw": bool(args.dump_polymarket_raw)},
            name="polymarket-price-compare",
            daemon=True,
        ),
    ]
    for th in threads:
        th.start()

    started = time.time()
    try:
        while True:
            _print_status(state)
            if args.duration_sec > 0 and (time.time() - started) >= float(args.duration_sec):
                break
            time.sleep(max(0.2, float(args.print_interval_sec)))
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for th in threads:
            th.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
