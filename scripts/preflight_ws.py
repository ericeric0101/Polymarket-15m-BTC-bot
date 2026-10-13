"""Pre-launch network + disk preflight for the Entry V3 DRY-RUN smoke (read-only, public endpoints only).

Steps: DNS resolution -> TLS handshake (certificate VERIFIED; never bypassed) -> public read-only
lookup of the current BTC 15m market token (gamma GET) -> public CLOB market WebSocket connect ->
subscribe -> at least one valid market-data message -> clean close.  Disk: free space vs the
DRY-RUN minimum (13 GiB), the 7+ day LIVE-shadow minimum (20 GiB) and the bot guard (10 GiB).

Never loads credentials or .env, never calls an order/account endpoint, never prints secrets,
never deletes anything.  Hard wall-clock budget ~60 s.
Usage: python -I scripts/preflight_ws.py [--asset-id <token id>] [--out <json>]
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import ssl
import sys
import time
import urllib.request
from typing import Any, Callable, Optional

CLOB_WS_HOST = "ws-subscriptions-clob.polymarket.com"
CLOB_WS_URL = f"wss://{CLOB_WS_HOST}/ws/market"
GAMMA_HOST = "gamma-api.polymarket.com"
TOTAL_BUDGET_SEC = 60.0
DRY_RUN_MIN_FREE_GIB = 13.0
LIVE_SHADOW_MIN_FREE_GIB = 20.0
BOT_GUARD_GIB = 10.0
VALID_EVENT_TYPES = {"book", "price_change", "last_trade_price", "tick_size_change", "best_bid_ask"}


def check_dns(host: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> dict:
    try:
        infos = resolver(host, 443, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        return {"step": "DNS", "ok": bool(addrs), "host": host, "addresses": addrs}
    except Exception as exc:
        return {"step": "DNS", "ok": False, "host": host, "error": type(exc).__name__}


def check_tls(host: str, *, timeout: float, connector: Optional[Callable[..., Any]] = None) -> dict:
    """Full certificate + hostname verification with the system trust store."""
    ctx = ssl.create_default_context()
    try:
        if connector is not None:
            info = connector(host, ctx, timeout)
        else:
            with socket.create_connection((host, 443), timeout=timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as tls:
                    cert = tls.getpeercert() or {}
                    subject = dict(x[0] for x in cert.get("subject", ()))
                    issuer = dict(x[0] for x in cert.get("issuer", ()))
                    info = {"tls_version": tls.version(), "subject_cn": subject.get("commonName"),
                            "issuer_cn": issuer.get("commonName"), "not_after": cert.get("notAfter")}
        return {"step": "TLS", "ok": True, "host": host, **info}
    except ssl.SSLCertVerificationError as exc:
        return {"step": "TLS", "ok": False, "host": host, "error": "CERTIFICATE_VERIFY_FAILED",
                "detail": str(getattr(exc, "verify_message", ""))[:120]}
    except Exception as exc:
        return {"step": "TLS", "ok": False, "host": host, "error": type(exc).__name__}


def current_btc15m_slug(now: float) -> str:
    return f"btc-updown-15m-{int(now // 900 * 900)}"


def lookup_asset_id(now: float, *, timeout: float, fetch: Optional[Callable[[str, float], bytes]] = None) -> dict:
    """Public, unauthenticated read-only GET of the current market's first CLOB token id."""
    slug = current_btc15m_slug(now)
    url = f"https://{GAMMA_HOST}/markets?slug={slug}"

    def _get(u: str, t: float) -> bytes:
        req = urllib.request.Request(u, headers={"User-Agent": "preflight-ws/1"})
        with urllib.request.urlopen(req, timeout=t, context=ssl.create_default_context()) as resp:
            return resp.read()
    try:
        data = json.loads((fetch or _get)(url, timeout))
        market = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        tokens = market.get("clobTokenIds")
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
        asset = str(tokens[0]) if tokens else None
        return {"step": "MARKET_LOOKUP", "ok": bool(asset), "slug": slug, "asset_id": asset}
    except Exception as exc:
        return {"step": "MARKET_LOOKUP", "ok": False, "slug": slug, "error": type(exc).__name__}


def _valid_market_message(raw: Any) -> bool:
    try:
        msg = json.loads(raw)
    except Exception:
        return False
    items = msg if isinstance(msg, list) else [msg]
    return any(isinstance(m, dict) and (m.get("event_type") in VALID_EVENT_TYPES or "bids" in m or "price_changes" in m)
               for m in items)


def check_ws(asset_id: str, *, deadline: float, connect: Optional[Callable[..., Any]] = None,
             clock: Callable[[], float] = time.monotonic) -> dict:
    """Connect, subscribe to the public market channel, wait for one valid message, close cleanly."""
    if connect is None:
        from websockets.sync.client import connect as ws_connect

        def connect(url, timeout):
            return ws_connect(url, open_timeout=timeout, close_timeout=5, ssl=ssl.create_default_context())
    out: dict[str, Any] = {"step": "WEBSOCKET", "ok": False, "url": CLOB_WS_URL, "messages_seen": 0}
    t0 = clock()
    try:
        ws = connect(CLOB_WS_URL, max(1.0, deadline - clock()))
        out["connected"] = True
        try:
            ws.send(json.dumps({"assets_ids": [asset_id], "type": "market"}))
            out["subscribed"] = True
            while clock() < deadline:
                try:
                    raw = ws.recv(timeout=max(0.5, min(5.0, deadline - clock())))
                except TimeoutError:
                    continue
                out["messages_seen"] += 1
                if _valid_market_message(raw):
                    out["ok"] = True
                    out["first_valid_message_after_sec"] = round(clock() - t0, 3)
                    break
        finally:
            ws.close()
            out["closed_cleanly"] = True
        if not out["ok"]:
            out["error"] = "NO_VALID_MESSAGE_BEFORE_DEADLINE"
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def check_disk(path: str = "/", usage: Callable[[str], Any] = shutil.disk_usage) -> dict:
    free_gib = usage(path).free / 1024 ** 3
    return {"step": "DISK", "ok": free_gib >= DRY_RUN_MIN_FREE_GIB, "free_gib": round(free_gib, 2),
            "dry_run_min_gib": DRY_RUN_MIN_FREE_GIB, "live_shadow_min_gib": LIVE_SHADOW_MIN_FREE_GIB,
            "bot_guard_gib": BOT_GUARD_GIB, "meets_dry_run_min": free_gib >= DRY_RUN_MIN_FREE_GIB,
            "meets_live_shadow_min": free_gib >= LIVE_SHADOW_MIN_FREE_GIB, "above_bot_guard": free_gib >= BOT_GUARD_GIB}


def run_preflight(*, asset_id: Optional[str] = None, resolver=socket.getaddrinfo, tls_connector=None, fetch=None,
                  ws_connect=None, usage=shutil.disk_usage, clock: Callable[[], float] = time.monotonic,
                  wall: Callable[[], float] = time.time) -> dict:
    deadline = clock() + TOTAL_BUDGET_SEC
    steps = [check_dns(CLOB_WS_HOST, resolver)]
    if steps[-1]["ok"]:
        steps.append(check_tls(CLOB_WS_HOST, timeout=10.0, connector=tls_connector))
    if steps[-1]["ok"] and not asset_id:
        steps.append(check_dns(GAMMA_HOST, resolver))
        if steps[-1]["ok"]:
            lookup = lookup_asset_id(wall(), timeout=10.0, fetch=fetch)
            steps.append(lookup)
            asset_id = lookup.get("asset_id")
    if steps[-1]["ok"] and asset_id:
        steps.append(check_ws(asset_id, deadline=deadline, connect=ws_connect, clock=clock))
    network_ok = all(s["ok"] for s in steps) and steps[-1]["step"] == "WEBSOCKET"
    disk = check_disk(usage=usage)
    return {"preflight": "ws_and_disk_v1", "network_ok": network_ok, "disk": disk, "steps": steps,
            "result": "PASS" if network_ok and disk["ok"] else "FAIL",
            "credentials_loaded": False, "order_endpoints_called": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--asset-id", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    report = run_preflight(asset_id=args.asset_id)
    text = json.dumps(report, indent=1, sort_keys=True)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
