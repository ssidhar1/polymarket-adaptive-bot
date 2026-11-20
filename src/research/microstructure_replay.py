"""Offline microstructure candidate generation over the public CLOB journal.

Signals are exploratory paper intents only. The runner uses only snapshots and
deltas received by each decision timestamp and delegates delayed book-walk
execution to CLOBReplayEngine.
"""
import argparse
import json
from src.execution.clob_replay import CLOBReplayEngine
from src.signals.microstructure import compute_microstructure_features


def generate_microstructure_decisions(
    journal,
    token_ids,
    *,
    market_ids=None,
    resolution_prices=None,
    fee_schedules=None,
    notional_usd=5.0,
    min_ofi_normalized=0.15,
    min_microprice_edge_bps=1.0,
    max_spread_bps=500.0,
    min_total_depth_shares=1.0,
    cooldown_ms=1000,
    default_fee_rate=0.07,
    default_fee_exponent=1.0,
    default_fee_collection="usdc",
):
    market_ids = market_ids or {}
    resolution_prices = resolution_prices or {}
    fee_schedules = fee_schedules or {}
    cooldown_ns = max(0, int(float(cooldown_ms) * 1_000_000))
    previous_books = {}
    last_signal_ns = {}
    decisions = []
    observations = 0

    for token_id in sorted({str(t).strip() for t in token_ids if str(t).strip()}):
        for received_ns in journal.book_update_times(token_id):
            book = journal.replay_book(token_id, received_before_ns=received_ns)
            if not book.get("complete") or not book.get("has_snapshot"):
                previous_books.pop(token_id, None)
                continue
            # A batch event may concern another token. Only evaluate when this
            # exact receive-time cutoff actually changed this token's book.
            if book.get("latest_book_update_received_ns") != received_ns:
                continue
            features = compute_microstructure_features(book, previous_books.get(token_id))
            previous_books[token_id] = book
            observations += 1
            if not features.get("valid"):
                continue
            ofi = features.get("ofi_normalized")
            micro_edge = features.get("microprice_edge_bps")
            total_depth = float(features.get("bid_depth", 0)) + float(features.get("ask_depth", 0))
            if ofi is None or micro_edge is None:
                continue
            if float(ofi) < float(min_ofi_normalized):
                continue
            if float(micro_edge) < float(min_microprice_edge_bps):
                continue
            if float(features.get("spread_bps", float("inf"))) > float(max_spread_bps):
                continue
            if total_depth < float(min_total_depth_shares):
                continue
            prior_signal = last_signal_ns.get(token_id)
            if prior_signal is not None and received_ns - prior_signal < cooldown_ns:
                continue

            schedule = fee_schedules.get(token_id) or {}
            intent = {
                "decision_id": f"{token_id}:{received_ns}",
                "token_id": token_id,
                "market_id": str(market_ids.get(token_id) or token_id),
                "decision_received_ns": int(received_ns),
                "notional_usd": float(notional_usd),
                "fee_rate": float(schedule.get("rate", default_fee_rate)),
                "fee_exponent": float(schedule.get("exponent", default_fee_exponent)),
                "fee_collection": schedule.get("collection", default_fee_collection),
                "fee_source": schedule.get("source", "config_fallback"),
                "fee_verified": bool(schedule.get("verified", False)),
                "signal": "positive_ofi_microprice_confirmation",
                "signal_features": features,
            }
            if token_id in resolution_prices:
                intent["resolution_price"] = float(resolution_prices[token_id])
            decisions.append(intent)
            last_signal_ns[token_id] = received_ns

    return {"observations": observations, "decisions": decisions}


def run_microstructure_research(
    journal,
    token_ids,
    *,
    decision_latency_ms=250,
    max_book_age_ms=2000,
    slippage_bps=8,
    max_slippage_bps=30,
    default_fee_rate=0.07,
    default_fee_exponent=1.0,
    default_fee_collection="usdc",
    **strategy_kwargs,
):
    generated = generate_microstructure_decisions(
        journal, token_ids, default_fee_rate=default_fee_rate,
        default_fee_exponent=default_fee_exponent,
        default_fee_collection=default_fee_collection, **strategy_kwargs
    )
    replay = CLOBReplayEngine(
        journal,
        decision_latency_ms=decision_latency_ms,
        max_book_age_ms=max_book_age_ms,
        slippage_bps=slippage_bps,
        max_slippage_bps=max_slippage_bps,
        default_fee_rate=default_fee_rate,
        default_fee_exponent=default_fee_exponent,
        default_fee_collection=default_fee_collection,
    ).replay(generated["decisions"])
    return {
        "observations": generated["observations"],
        "candidate_signals": len(generated["decisions"]),
        "execution_replay": replay,
        "research_only": True,
        "strategy": "positive_ofi_microprice_confirmation",
    }



def main():
    parser = argparse.ArgumentParser(description="Replay a simple OFI/microprice paper baseline")
    parser.add_argument("--db", default="data/clob_events.sqlite")
    parser.add_argument("--tokens", required=True, help="Comma-separated CLOB outcome token IDs")
    parser.add_argument("--notional", type=float, default=5.0)
    parser.add_argument("--fee-rate", type=float, default=0.07,
                        help="Conservative fallback; market-specific fee schedules can be supplied by Python API")
    parser.add_argument("--fee-collection", choices=["usdc", "shares"], default="usdc")
    parser.add_argument("--latency-ms", type=float, default=250)
    parser.add_argument("--max-book-age-ms", type=float, default=2000)
    parser.add_argument("--slippage-bps", type=float, default=8)
    parser.add_argument("--max-slippage-bps", type=float, default=30)
    parser.add_argument("--min-ofi", type=float, default=0.15)
    parser.add_argument("--min-microprice-bps", type=float, default=1.0)
    parser.add_argument("--max-spread-bps", type=float, default=500)
    parser.add_argument("--cooldown-ms", type=float, default=1000)
    args = parser.parse_args()

    from src.data.market_event_journal import MarketEventJournal
    journal = MarketEventJournal(args.db)
    tokens = [token.strip() for token in args.tokens.split(",") if token.strip()]
    from src.data.polymarket_client import PolymarketData
    data = PolymarketData()
    fee_schedules = {
        token: data.get_fee_schedule(token, default_rate=args.fee_rate)
        for token in tokens
    }
    if args.fee_collection == "shares":
        for schedule in fee_schedules.values():
            schedule["collection"] = "shares"
    result = run_microstructure_research(
        journal, tokens, fee_schedules=fee_schedules,
        notional_usd=args.notional,
        min_ofi_normalized=args.min_ofi,
        min_microprice_edge_bps=args.min_microprice_bps,
        max_spread_bps=args.max_spread_bps,
        cooldown_ms=args.cooldown_ms,
        decision_latency_ms=args.latency_ms,
        max_book_age_ms=args.max_book_age_ms,
        slippage_bps=args.slippage_bps,
        max_slippage_bps=args.max_slippage_bps,
        default_fee_rate=args.fee_rate,
        default_fee_collection=args.fee_collection,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
