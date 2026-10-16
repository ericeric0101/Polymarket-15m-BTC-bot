"""Authoritative per-market PnL attribution from the trade journal."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def _payload(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _empty_market_pnl(slug: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "buy_notional_usdc": 0.0, "buy_fee_usdc": 0.0,
        "maker_sell_proceeds_usdc": 0.0, "taker_exit_proceeds_usdc": 0.0,
        "sell_fee_usdc": 0.0, "redeem_value_usdc": 0.0,
        "redeem_value_source": "settlement_estimate", "fill_count": 0,
        "buy_fill_count": 0, "maker_sell_fill_count": 0,
        "taker_exit_fill_count": 0, "reported_cycle_pnl_usdc": None,
        "computed_pnl_usdc": 0.0, "attributable_pnl_usdc": None,
        "accounting_status": "no_tracked_entry", "reconciliation_adjustment_usdc": None,
        "buy_qty": 0.0, "sell_qty": 0.0, "entry_sources": set(),
        "entry_source": None, "source_attribution_status": "no_tracked_entry",
        "settlement_recorded": False,
    }


def _entry_source(client_order_id: Any, payload: dict[str, Any]) -> str:
    """Classify an entry without treating unknown historic rows as Outcome."""
    explicit = str(payload.get("entry_source") or "").strip().lower()
    if explicit in {"outcome_fast_follow", "normal_maker"}:
        return explicit
    order_id = str(client_order_id or "")
    if order_id.startswith("BTC-15M-FAST-FOLLOW-BUY-"):
        return "outcome_fast_follow"
    if order_id.startswith("BTC-15M-MAKER-BUY-"):
        return "normal_maker"
    return "unknown"


def load_market_pnl_attributions(
    db_path: str | Path,
    slugs: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load a whole journal in bounded passes, preserving attribution status.

    A SELL without a journaled BUY is cash activity but not attributable bot
    PnL: the inventory predates this journal or was restored externally.  It
    stays visible as ``pre_journal_inventory`` instead of inflating strategy
    performance.  One set-based scan also avoids the old report's O(markets ×
    journal) JSON scans on large databases.
    """
    requested = {str(slug) for slug in slugs} if slugs else None
    result: dict[str, dict[str, Any]] = {}

    def item(slug: str) -> dict[str, Any]:
        if slug not in result:
            result[slug] = _empty_market_pnl(slug)
        return result[slug]

    with sqlite3.connect(str(db_path)) as conn:
        taker_ids: dict[str, set[str]] = defaultdict(set)
        for client_order_id, raw in conn.execute(
            "SELECT client_order_id, payload_json FROM order_events WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'"
        ):
            payload = _payload(raw)
            slug = str(payload.get("slug") or "")
            if slug and (requested is None or slug in requested):
                taker_ids[slug].add(str(client_order_id or ""))

        for client_order_id, side, price, qty, raw in conn.execute(
            """SELECT client_order_id, side, price, qty, payload_json
               FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id"""
        ):
            payload = _payload(raw)
            slug = str(payload.get("slug") or "")
            if not slug or (requested is not None and slug not in requested):
                continue
            row = item(slug)
            fill_side = str(side or "").upper()
            notional, fee = _num(price) * _num(qty), _num(payload.get("effective_fee_usdc"))
            row["fill_count"] += 1
            if fill_side == "BUY":
                row["buy_fill_count"] += 1
                row["buy_notional_usdc"] += notional
                row["buy_fee_usdc"] += fee
                row["buy_qty"] += _num(qty)
                row["entry_sources"].add(_entry_source(client_order_id, payload))
            elif fill_side == "SELL":
                row["sell_qty"] += _num(qty)
                if str(client_order_id or "") in taker_ids[slug]:
                    row["taker_exit_fill_count"] += 1
                    row["taker_exit_proceeds_usdc"] += notional
                else:
                    row["maker_sell_fill_count"] += 1
                    row["maker_sell_proceeds_usdc"] += notional
                row["sell_fee_usdc"] += fee

        redeem_seen: dict[str, set[str]] = defaultdict(set)
        settlement_redeem: dict[str, float] = {}
        for event_type, raw in conn.execute(
            """SELECT event_type, payload_json FROM strategy_events
               WHERE event_type IN ('REDEEM_EXECUTED', 'MARKET_SETTLEMENT', 'MARKET_CYCLE_PNL')
               ORDER BY id"""
        ):
            payload = _payload(raw)
            slug = str(payload.get("slug") or "")
            if not slug or (requested is not None and slug not in requested):
                continue
            row = item(slug)
            if event_type == "REDEEM_EXECUTED" and int(_num(payload.get("status"))) == 1:
                # A successful redemption transaction does not by itself
                # establish the USDC received.  Older rows only recorded
                # redeemed shares, so preserve the settlement-value fallback
                # rather than silently treating their cash value as zero.
                if "redeem_cash_usdc" not in payload:
                    continue
                identity = str(payload.get("condition_id") or payload.get("tx_hash") or payload.get("redeem_activity_tx_hash") or "")
                if identity and identity not in redeem_seen[slug]:
                    redeem_seen[slug].add(identity)
                    row["redeem_value_usdc"] += _num(payload.get("redeem_cash_usdc"))
                    row["redeem_value_source"] = "onchain_redeem"
            elif event_type == "MARKET_SETTLEMENT":
                settlement_redeem[slug] = _num(payload.get("redeem_value_usdc"))
                row["settlement_recorded"] = True
            elif event_type == "MARKET_CYCLE_PNL":
                row["reported_cycle_pnl_usdc"] = _num(payload.get("cycle_combined_pnl_usdc"))

        for slug, value in settlement_redeem.items():
            row = item(slug)
            if not redeem_seen[slug]:
                row["redeem_value_usdc"] = value

    for row in result.values():
        row["computed_pnl_usdc"] = (
            row["maker_sell_proceeds_usdc"] + row["taker_exit_proceeds_usdc"]
            + row["redeem_value_usdc"] - row["buy_notional_usdc"]
            - row["buy_fee_usdc"] - row["sell_fee_usdc"]
        )
        has_exit_cash = any(row[name] > 0 for name in (
            "maker_sell_proceeds_usdc", "taker_exit_proceeds_usdc", "redeem_value_usdc",
        ))
        if row["buy_fill_count"] == 0 and has_exit_cash:
            row["accounting_status"] = "pre_journal_inventory"
        elif row["buy_fill_count"] > 0:
            sources = row["entry_sources"]
            if len(sources) == 1:
                row["entry_source"] = next(iter(sources))
                row["source_attribution_status"] = "source_pure"
            else:
                row["source_attribution_status"] = "mixed_entry_sources"
            settled_or_fully_sold = bool(row["settlement_recorded"]) or (
                row["sell_qty"] + 1e-9 >= row["buy_qty"]
            )
            if not settled_or_fully_sold:
                row["accounting_status"] = "open_or_unreconciled"
            elif len(sources) != 1:
                row["accounting_status"] = "mixed_entry_sources"
            else:
                row["accounting_status"] = "complete"
                row["attributable_pnl_usdc"] = row["computed_pnl_usdc"]
                if row["reported_cycle_pnl_usdc"] is not None:
                    row["reconciliation_adjustment_usdc"] = (
                        row["reported_cycle_pnl_usdc"] - row["computed_pnl_usdc"]
                    )
    return result


def load_fast_follow_pnl_summary(db_path: str | Path) -> dict[str, Any]:
    """Return only completed, source-pure Outcome fast-follow PnL.

    Open positions and mixed-source markets are intentionally excluded. This
    is an audit ledger, never a position or settlement estimator.
    """
    rows = load_market_pnl_attributions(db_path)
    completed = [
        row for row in rows.values()
        if row["entry_source"] == "outcome_fast_follow"
        and row["accounting_status"] == "complete"
    ]
    return {
        "completed_trade_count": len(completed),
        "completed_pnl_usdc": sum(
            float(row["attributable_pnl_usdc"] or 0.0) for row in completed
        ),
        "excluded_open_or_unreconciled_count": sum(
            1 for row in rows.values()
            if row["entry_source"] == "outcome_fast_follow"
            and row["accounting_status"] == "open_or_unreconciled"
        ),
        "excluded_mixed_source_count": sum(
            1 for row in rows.values()
            if row["accounting_status"] == "mixed_entry_sources"
            and "outcome_fast_follow" in row["entry_sources"]
        ),
        "markets": completed,
    }


def load_market_pnl_attribution(db_path: str | Path, slug: str) -> dict[str, Any]:
    """Return a reproducible PnL ledger for one market.

    `computed_pnl_usdc` is derived only from fills and redemption value.  The
    journal's `MARKET_CYCLE_PNL` remains separately reported, so recovery after
    process restarts or external reconciliation cannot be silently hidden.
    """
    slug = str(slug or "")
    if not slug:
        return _empty_market_pnl("")
    return load_market_pnl_attributions(db_path, {slug}).get(slug, _empty_market_pnl(slug))


# ======================================================================================
# Effective PnL projection (single source for dashboard, reports and reconciliation)
# ======================================================================================
#
# Raw journal rows (fills, MARKET_SETTLEMENT, MARKET_CYCLE_PNL) are never edited
# by this projection. Official outcome and redeem cash arrive as append-only
# evidence (bot.settlement_evidence) and are deduped by evidence key.
#
#   PnL = sell net proceeds + payout - buy total cost       (fees counted once)
#
#   * buy total cost  = sum(price x gross qty) + USDC buy fees (protocol v2);
#     a v1 taker BUY fee is taken in shares, so it reduces held shares instead.
#   * sell net        = sum(price x qty) - USDC sell fees.
#   * held shares     = gross BUY qty - fee shares - SELL qty, per token.
#   * payout          = confirmed redeem cash when it matches the tracked
#     position, otherwise held winning shares x $1 under the best known outcome.
#
# Basis (strongest first), never mixed inside one total:
#   FILLS_FINAL        no shares held at settlement: PnL is fully cash-realized
#   CASH_CONFIRMED     held shares paid out; redeem cash matches the position
#   OUTCOME_CONFIRMED  official outcome known; payout computed, cash not yet seen
#   ESTIMATED          only the bot's own TWAP label is known
#   PENDING            outcome unknown (never treated as a zero payout)
#   OPEN               market not ended and shares are held
#   INCOMPLETE         data cannot support a PnL (see ``issues``); excluded

PNL_BASIS_ORDER = (
    "FILLS_FINAL", "CASH_CONFIRMED", "OUTCOME_CONFIRMED", "ESTIMATED", "PENDING", "OPEN", "INCOMPLETE",
)
FINAL_BASES = frozenset({"FILLS_FINAL", "CASH_CONFIRMED", "OUTCOME_CONFIRMED"})
SHARE_DUST = 0.02          # below this, held shares are rounding / fee-estimate dust
CASH_MATCH_TOLERANCE = 0.02  # USDC; redeem cash vs. computed payout
MARKET_DURATION_SEC = 900


def _slug_end_ts(slug: str) -> float | None:
    try:
        return float(int(str(slug).rsplit("-", 1)[1]) + MARKET_DURATION_SEC)
    except (IndexError, ValueError):
        return None


def _token_from_instrument(instrument_id: Any) -> str:
    text = str(instrument_id or "")
    if "-" not in text:
        return ""
    return text.split("-", 1)[1].split(".", 1)[0]


def _iso_epoch(ts: Any) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except (TypeError, ValueError):
        return None


def _match_venue_trades(journal_fills: list[dict[str, Any]], venue: list[dict[str, Any]]) -> tuple[list, list]:
    """Greedy match by (side, token, ~size, ~time). Returns (unmatched venue, unmatched journal)."""
    remaining = [f for f in journal_fills if f["price"] > 0]
    unmatched_venue = []
    for trade in sorted(venue, key=lambda t: _num(t.get("activity_timestamp"))):
        best = None
        for fill in remaining:
            if fill["side"] != trade["side"] or fill["token"] != str(trade.get("token_id") or ""):
                continue
            if abs(fill["qty"] - _num(trade.get("size"))) > max(0.25, 0.05 * fill["qty"]):
                continue
            if fill["ts"] is not None and abs(fill["ts"] - _num(trade.get("activity_timestamp"))) > 180:
                continue
            best = fill
            break
        if best is None:
            unmatched_venue.append(trade)
        else:
            remaining.remove(best)
    return unmatched_venue, remaining


def _submit_tokens(conn: sqlite3.Connection) -> dict[str, str]:
    """client_order_id -> token the order was submitted for (submit/intent rows)."""
    out: dict[str, str] = {}
    for client_order_id, token_id, raw in conn.execute(
        """SELECT client_order_id, token_id, payload_json FROM order_events
           WHERE event_type IN ('ORDER_SUBMIT', 'ORDER_TAKER_EXIT_SUBMIT', 'ORDER_MAKER_INTENT')
           ORDER BY id"""
    ):
        coid = str(client_order_id or "")
        if not coid or coid in out:
            continue
        payload = _payload(raw)
        token = str(token_id or "") or _token_from_instrument(
            payload.get("instrument_id") or payload.get("submitted_instrument_id")
        )
        if token:
            out[coid] = token
    return out


def _drop_zero_price_duplicate_fills(fills: list[tuple]) -> tuple[list[tuple], set[str]]:
    """Drop a 0-priced fill that duplicates a priced fill of the same order.

    Observed journal artefact: a SELL row at price 0 next to the real fill of the
    same client order (same second, ~same qty). It would double the sold
    quantity. A lone 0-priced fill is kept.
    """
    priced: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for fill in fills:
        _id, ts, client_order_id, _side, price, qty, *_rest = fill
        if _num(price) > 0:
            priced[str(client_order_id or "")].append((str(ts or "")[:19], _num(qty)))
    kept, dropped_slugs = [], set()
    for fill in fills:
        _id, ts, client_order_id, _side, price, qty, _tok, _inst, raw = fill
        if _num(price) == 0 and any(
            same_ts == str(ts or "")[:19] and abs(other_qty - _num(qty)) <= 0.1
            for same_ts, other_qty in priced.get(str(client_order_id or ""), [])
        ):
            dropped_slugs.add(str(_payload(raw).get("slug") or ""))
            continue
        kept.append(fill)
    return kept, dropped_slugs


def _relabel_orphan_sibling_sells(tokens: dict[str, dict[str, float]], issues: list[str]) -> dict[str, dict[str, float]]:
    """A SELL booked on the market's never-bought sibling token belongs to the bought token.

    Observed journal artefact on taker exits. Only applied when exactly one token
    was bought and the orphan SELL quantity fits that token's held shares; the
    adjustment is reported, never silent.
    """
    bought = [token for token, t in tokens.items() if t["buy_qty"] > 0]
    orphans = [token for token, t in tokens.items() if t["buy_qty"] <= 0 and t["sell_qty"] > 0]
    if len(bought) != 1 or not orphans:
        return tokens
    target = tokens[bought[0]]
    held = target["buy_qty"] - target["fee_shares"] - target["sell_qty"]
    orphan_qty = sum(tokens[token]["sell_qty"] for token in orphans)
    if orphan_qty > held + SHARE_DUST:
        return tokens
    relabelled = {token: dict(t) for token, t in tokens.items() if token not in orphans}
    relabelled[bought[0]]["sell_qty"] += orphan_qty
    issues.append("sell_token_relabelled_inferred")
    return relabelled


def _load_evidence_rows(conn: sqlite3.Connection) -> list[tuple[str, dict[str, Any]]]:
    from bot.settlement_evidence import EVIDENCE_EVENTS
    rows = conn.execute(
        f"""SELECT event_type, payload_json FROM strategy_events
            WHERE event_type IN ({','.join('?' * len(EVIDENCE_EVENTS))}) ORDER BY id""",
        EVIDENCE_EVENTS,
    )
    return [(str(event_type), _payload(raw)) for event_type, raw in rows]


def load_effective_market_pnl(
    db_path: str | Path,
    *,
    evidence: list[dict[str, Any]] | None = None,
    as_of_ts: float | None = None,
    slugs: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-market effective PnL from fills + journaled/externally supplied evidence.

    ``evidence`` lets a reconciliation or dry-run supply fetched evidence payloads
    without writing them (deduped with journaled evidence by key). Fills are
    keyed to a market by token identity (official token map) when known, not by
    the slug that happened to be current when the fill was journaled. Opens the
    journal read-only.
    """
    import time as _time
    from bot.settlement_evidence import OUTCOME_EVENT, TRADE_EVENT, evidence_event_type

    now = float(as_of_ts if as_of_ts is not None else _time.time())
    requested = {str(s) for s in slugs} if slugs else None
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        fills = conn.execute(
            """SELECT id, ts, client_order_id, side, price, qty, token_id, instrument_id, payload_json
               FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id"""
        ).fetchall()
        strategy_rows = conn.execute(
            """SELECT id, ts, event_type, payload_json FROM strategy_events
               WHERE event_type IN ('MARKET_SETTLEMENT', 'MARKET_CYCLE_PNL', 'REDEEM_EXECUTED') ORDER BY id"""
        ).fetchall()
        journal_evidence = _load_evidence_rows(conn)
        submit_token = _submit_tokens(conn)
    finally:
        conn.close()

    # 1. Evidence (journal first, then supplied), deduped by key.
    seen_keys: set[str] = set()
    outcomes: dict[str, dict[str, Any]] = {}
    redeems: list[dict[str, Any]] = []
    venue_trades: list[dict[str, Any]] = []
    supplied = [(evidence_event_type(e), e) for e in (evidence or [])]
    for event_type, payload in journal_evidence + supplied:
        key = str(payload.get("evidence_key") or "")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        if event_type == OUTCOME_EVENT:
            if payload.get("resolved") and payload.get("official_outcome") in ("UP", "DOWN"):
                outcomes[str(payload.get("slug") or "")] = payload
        elif event_type == TRADE_EVENT:
            venue_trades.append(payload)
        else:
            redeems.append(payload)
    slug_by_token = {token: slug for slug, o in outcomes.items() for token in (o.get("token_outcomes") or {})}
    slug_by_condition = {o.get("condition_id"): s for s, o in outcomes.items() if o.get("condition_id")}

    markets: dict[str, dict[str, Any]] = {}

    def market(slug: str) -> dict[str, Any]:
        if slug not in markets:
            markets[slug] = {
                "slug": slug, "market_end_ts": _slug_end_ts(slug), "tokens": {},
                "buy_cost_usdc": 0.0, "buy_notional_usdc": 0.0, "sell_net_usdc": 0.0,
                "buy_fill_count": 0, "sell_fill_count": 0,
                "first_fill_ts": None, "last_fill_ts": None,
                "settlement": None, "settlement_ts": None, "cycle_pnl": None, "cycle_pnl_source": None,
                "legacy_redeem": [], "redeems": [], "venue_trades": [], "journal_fills": [], "issues": [],
            }
        return markets[slug]

    def wanted(slug: str) -> bool:
        return bool(slug) and (requested is None or slug in requested)

    # 2. Fills, keyed by (corrected) token identity.
    fills, dropped_zero_price = _drop_zero_price_duplicate_fills(fills)
    for slug in dropped_zero_price:
        if wanted(slug):
            market(slug)["issues"].append("zero_price_duplicate_fill_dropped")
    for _id, ts, client_order_id, side, price, qty, token_id, instrument_id, raw in fills:
        payload = _payload(raw)
        journal_slug = str(payload.get("slug") or payload.get("market_slug") or "")
        token = str(token_id or "") or _token_from_instrument(instrument_id or payload.get("instrument_id"))
        corrected = False
        submitted = submit_token.get(str(client_order_id or ""))
        if submitted and submitted != token:
            # Observed journal artefact: some taker-exit fills carry the sibling
            # token. The order's own submit/intent row is the authority.
            token, corrected = submitted, True
        slug = slug_by_token.get(token) or journal_slug
        if not wanted(slug):
            continue
        row = market(slug)
        if corrected:
            row["issues"].append("fill_token_corrected_from_submit")
        if journal_slug and slug != journal_slug:
            row["issues"].append("fill_reattributed_by_token")
        tok = row["tokens"].setdefault(token, {"buy_qty": 0.0, "fee_shares": 0.0, "sell_qty": 0.0})
        q, p = _num(qty), _num(price)
        fill_side = str(side or "").upper()
        row["journal_fills"].append({"side": fill_side, "token": token, "qty": q, "price": p, "ts": _iso_epoch(ts)})
        if fill_side == "BUY":
            row["buy_fill_count"] += 1
            tok["buy_qty"] += q
            tok["fee_shares"] += _num(payload.get("effective_fee_shares"))
            row["buy_cost_usdc"] += p * q + _num(payload.get("effective_fee_usdc"))
            row["buy_notional_usdc"] += p * q
        elif fill_side == "SELL":
            row["sell_fill_count"] += 1
            tok["sell_qty"] += q
            row["sell_net_usdc"] += p * q - _num(payload.get("effective_fee_usdc"))
        row["first_fill_ts"] = row["first_fill_ts"] or ts
        row["last_fill_ts"] = ts

    # 3. Journal settlement / raw cycle PnL / legacy redeem rows (raw, unedited).
    for _id, ts, event_type, raw in strategy_rows:
        payload = _payload(raw)
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not wanted(slug):
            continue
        if event_type == "MARKET_SETTLEMENT":
            if slug in markets or _num(payload.get("inventory_shares")) > 0:
                row = market(slug)
                row["settlement"], row["settlement_ts"] = payload, ts
        elif event_type == "MARKET_CYCLE_PNL":
            if slug in markets:
                row = market(slug)
                value = payload.get("cycle_combined_pnl_usdc")
                row["cycle_pnl"] = None if value is None else _num(value)
                row["cycle_pnl_source"] = payload.get("cycle_pnl_reconciled_source") or payload.get("source") or "settlement"
        elif event_type == "REDEEM_EXECUTED" and "redeem_cash_usdc" in payload:
            market(slug)["legacy_redeem"].append(payload)

    # 4. Redeem cash evidence, keyed by condition (preferred) or slug.
    for item in redeems:
        slug = slug_by_condition.get(item.get("condition_id")) or str(item.get("slug") or "")
        if wanted(slug):
            market(slug)["redeems"].append(item)
    evidence_tx_conditions = {(r.get("tx_hash"), r.get("condition_id")) for r in redeems}
    for item in venue_trades:
        slug = slug_by_token.get(str(item.get("token_id") or "")) or str(item.get("slug") or "")
        if slug in markets:  # venue rows only enrich markets the bot journaled
            markets[slug]["venue_trades"].append(item)

    return {
        slug: _project_market(row, official=outcomes.get(slug), evidence_tx_conditions=evidence_tx_conditions, now=now)
        for slug, row in markets.items()
    }


def _project_market(row: dict[str, Any], *, official: dict[str, Any] | None,
                    evidence_tx_conditions: set, now: float) -> dict[str, Any]:
    from bot.settlement_evidence import _norm_hex
    issues = list(row["issues"])
    settlement = row["settlement"] or {}
    tokens = _relabel_orphan_sibling_sells(row["tokens"], issues)
    buy_cost, sell_net = row["buy_cost_usdc"], row["sell_net_usdc"]
    journal_buy_cost, journal_sell_net = buy_cost, sell_net
    cash_source = "journal_fills"
    if row["venue_trades"]:
        # The venue's cash legs are authoritative; the journal is compared, not trusted.
        unmatched_venue, unmatched_journal = _match_venue_trades(row["journal_fills"], row["venue_trades"])
        if any(t["side"] == "BUY" for t in unmatched_venue):
            issues.append("venue_buy_not_in_journal")
        if any(t["side"] == "SELL" for t in unmatched_venue):
            issues.append("venue_sell_not_in_journal")
        if unmatched_journal:
            issues.append("journal_fill_not_in_venue")
        venue_tokens: dict[str, dict[str, float]] = {}
        buy_cost = sell_net = 0.0
        for trade in row["venue_trades"]:
            tok = venue_tokens.setdefault(str(trade.get("token_id") or ""), {"buy_qty": 0.0, "fee_shares": 0.0, "sell_qty": 0.0})
            if trade["side"] == "BUY":
                tok["buy_qty"] += _num(trade.get("size"))
                buy_cost += _num(trade.get("usdc_size"))
            else:
                tok["sell_qty"] += _num(trade.get("size"))
                sell_net += _num(trade.get("usdc_size"))
        tokens, cash_source = venue_tokens, "venue_activity"
        issues = [i for i in issues if i not in ("sell_token_relabelled_inferred",)]
    elif any(f["price"] <= 0 for f in row["journal_fills"]):
        issues.append("fill_price_missing")
    gross_open = {token: t["buy_qty"] - t["sell_qty"] for token, t in tokens.items()}
    held_est = {token: t["buy_qty"] - t["fee_shares"] - t["sell_qty"] for token, t in tokens.items()}
    bought_tokens = [token for token, t in tokens.items() if t["buy_qty"] > 0]
    sell_count = sum(1 for t in row["venue_trades"] if t["side"] == "SELL") if cash_source == "venue_activity" \
        else row["sell_fill_count"]
    if not row["buy_fill_count"]:
        issues.append("no_journaled_buy")
    if len(bought_tokens) > 1:
        issues.append("multi_token")
    if any(qty < -SHARE_DUST for qty in held_est.values()):
        issues.append("sell_exceeds_tracked_buys")

    ended = row["market_end_ts"] is not None and now >= row["market_end_ts"]
    bot_outcome = str(settlement.get("outcome") or "").upper()
    bot_outcome = bot_outcome if bot_outcome in ("UP", "DOWN") else ""
    official_outcome = str((official or {}).get("official_outcome") or "")
    if official:
        outcome, outcome_state = official_outcome, "OFFICIAL"
    elif bot_outcome:
        outcome, outcome_state = bot_outcome, "ESTIMATED_TWAP"
    elif ended:
        outcome, outcome_state = "", "UNKNOWN"
    else:
        outcome, outcome_state = "", "NOT_ENDED"
    outcome_conflict = bool(official and bot_outcome and bot_outcome != official_outcome)

    token_outcomes = dict((official or {}).get("token_outcomes") or {})
    if not token_outcomes and len(tokens) == 1:
        side = str(settlement.get("inventory_side") or "").upper()
        if side in ("UP", "DOWN"):
            token_outcomes = {next(iter(tokens)): side}

    # Redeem cash: Data API evidence plus legacy REDEEM_EXECUTED cash not already evidenced.
    cash_items = list(row["redeems"])
    for legacy in row["legacy_redeem"]:
        pair = (_norm_hex(legacy.get("tx_hash") or legacy.get("redeem_activity_tx_hash")),
                _norm_hex(legacy.get("condition_id") or legacy.get("redeem_activity_condition_id")))
        if pair not in evidence_tx_conditions:
            cash_items.append({"redeem_cash_usdc": legacy.get("redeem_cash_usdc"), "tx_hash": pair[0],
                               "token_id": "", "redeemed_shares": None, "source": "legacy_redeem_executed"})
    redeem_cash = sum(_num(item.get("redeem_cash_usdc")) for item in cash_items) if cash_items else None
    redeemed_by_token: dict[str, float] = defaultdict(float)
    for item in cash_items:
        if item.get("token_id") and item.get("redeemed_shares") is not None:
            redeemed_by_token[str(item["token_id"])] += _num(item.get("redeemed_shares"))

    # Held shares: the fee-share estimate is replaced by the venue's own count
    # when a redeem reports the shares actually burned for that token.
    held: dict[str, float] = {}
    for token in tokens:
        if token in redeemed_by_token:
            actual = redeemed_by_token[token]
            if actual > gross_open[token] + SHARE_DUST:
                issues.append("redeem_includes_untracked_shares")
                held[token] = max(0.0, held_est[token])
            else:
                if abs(actual - max(0.0, held_est[token])) > SHARE_DUST:
                    issues.append("fee_share_estimate_corrected_by_redeem")
                held[token] = actual
        else:
            held[token] = max(0.0, held_est[token])
    held = {token: qty for token, qty in held.items() if qty > SHARE_DUST}
    held_shares = sum(held.values())
    unmapped = [token for token in held if token not in token_outcomes]
    entry_sides = {token_outcomes.get(token) for token in bought_tokens if token_outcomes.get(token)}
    if not entry_sides and str(settlement.get("inventory_side") or "").upper() in ("UP", "DOWN"):
        entry_sides = {str(settlement["inventory_side"]).upper()}

    expected_payout = None
    if held_shares <= 0:
        expected_payout = 0.0
    elif outcome and not unmapped:
        expected_payout = sum(qty for token, qty in held.items() if token_outcomes.get(token) == outcome)
    elif outcome and unmapped:
        issues.append("held_token_without_outcome_mapping")
    cash_matches = (
        redeem_cash is not None and expected_payout is not None
        and abs(redeem_cash - expected_payout) <= CASH_MATCH_TOLERANCE
    )
    if redeem_cash is not None and expected_payout is not None and not cash_matches:
        issues.append("redeem_cash_differs_from_tracked_position")

    if not bought_tokens:
        position_state = "NO_TRACKED_ENTRY"
    elif held_shares <= 0:
        position_state = "FULLY_SOLD" if sell_count else "NO_SHARES_HELD"
    else:
        position_state = "PARTIALLY_SOLD_HELD" if sell_count else "HELD"
    if held_shares <= 0:
        redeem_state = "REDEEMED" if redeem_cash else "NOT_REQUIRED"
    elif not ended:
        redeem_state = "NOT_ENDED"
    elif redeem_cash is not None:
        redeem_state = "REDEEMED"
    elif expected_payout is None:
        redeem_state = "AWAITING_OUTCOME"
    elif expected_payout > 0:
        redeem_state = "CLAIMABLE"
    else:
        redeem_state = "NOTHING_TO_CLAIM"

    blocking = {"no_journaled_buy", "sell_exceeds_tracked_buys", "held_token_without_outcome_mapping",
                "redeem_includes_untracked_shares", "venue_buy_not_in_journal", "fill_price_missing"}
    payout = None
    if any(issue in blocking for issue in issues):
        basis = "INCOMPLETE"
    elif held_shares <= 0:
        basis, payout = "FILLS_FINAL", 0.0
    elif not ended:
        basis = "OPEN"
    elif cash_matches:
        basis, payout = "CASH_CONFIRMED", redeem_cash
    elif outcome_state == "OFFICIAL" and expected_payout is not None:
        basis, payout = "OUTCOME_CONFIRMED", expected_payout
    elif outcome_state == "ESTIMATED_TWAP" and expected_payout is not None:
        basis, payout = "ESTIMATED", expected_payout
    else:
        basis = "PENDING"
    effective = None if payout is None else sell_net + payout - buy_cost
    raw_cycle = row["cycle_pnl"]
    buy_qty = sum(t["buy_qty"] for t in tokens.values())
    return {
        "slug": row["slug"],
        "market_end_ts": row["market_end_ts"],
        "condition_id": (official or {}).get("condition_id") or "",
        "tokens": sorted(tokens),
        "cash_source": cash_source,
        "buy_cost_usdc": buy_cost,
        "sell_net_usdc": sell_net,
        "journal_buy_cost_usdc": journal_buy_cost,
        "journal_sell_net_usdc": journal_sell_net,
        "buy_qty": buy_qty,
        "fee_shares": sum(t["fee_shares"] for t in tokens.values()),
        "sell_qty": sum(t["sell_qty"] for t in tokens.values()),
        "entry_vwap": row["buy_notional_usdc"] / buy_qty if buy_qty > 0 else None,
        "held_shares": held_shares,
        "buy_fill_count": row["buy_fill_count"],
        "sell_fill_count": row["sell_fill_count"],
        "entry_side": next(iter(entry_sides)) if len(entry_sides) == 1 else ("MIXED" if entry_sides else None),
        "position_state": position_state,
        "outcome_state": outcome_state,
        "outcome": outcome or None,
        "bot_outcome": bot_outcome or None,
        "outcome_conflict": outcome_conflict,
        "redeem_state": redeem_state,
        "expected_payout_usdc": expected_payout,
        "redeem_cash_usdc": redeem_cash,
        "redeem_evidence_count": len(cash_items),
        "pnl_basis": basis,
        "payout_usdc": payout,
        "effective_pnl_usdc": effective,
        "is_final": basis in FINAL_BASES,
        "journal_cycle_pnl_usdc": raw_cycle,
        "journal_cycle_pnl_source": row["cycle_pnl_source"],
        "journal_vs_effective_usdc": (
            None if raw_cycle is None or effective is None else raw_cycle - effective
        ),
        "settlement_recorded": bool(row["settlement"]),
        "settlement_pending": bool(settlement.get("settlement_pending")),
        "issues": sorted(set(issues)),
        "first_fill_ts": row["first_fill_ts"],
        "last_fill_ts": row["last_fill_ts"],
    }


def summarize_effective_pnl(rows: dict[str, dict[str, Any]] | list[dict[str, Any]]) -> dict[str, Any]:
    """One aggregation for every consumer: totals are sums of the same per-market rows."""
    items = list(rows.values()) if isinstance(rows, dict) else list(rows)
    by_basis: dict[str, dict[str, float]] = {b: {"count": 0, "pnl_usdc": 0.0} for b in PNL_BASIS_ORDER}
    for item in items:
        bucket = by_basis[item["pnl_basis"]]
        bucket["count"] += 1
        if item["effective_pnl_usdc"] is not None:
            bucket["pnl_usdc"] += item["effective_pnl_usdc"]
    final_pnl = sum(by_basis[b]["pnl_usdc"] for b in FINAL_BASES)
    return {
        "market_count": len(items),
        "by_basis": by_basis,
        "final_pnl_usdc": final_pnl,
        "estimated_pnl_usdc": by_basis["ESTIMATED"]["pnl_usdc"],
        "final_plus_estimated_pnl_usdc": final_pnl + by_basis["ESTIMATED"]["pnl_usdc"],
        "unresolved_count": sum(by_basis[b]["count"] for b in ("PENDING", "OPEN", "INCOMPLETE")),
        "first_market_end_ts": min((i["market_end_ts"] for i in items if i["market_end_ts"]), default=None),
        "last_market_end_ts": max((i["market_end_ts"] for i in items if i["market_end_ts"]), default=None),
    }
