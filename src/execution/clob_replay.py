"""Deterministic event-time CLOB replay for paper-only research.

Decisions are timestamped in local receive-time nanoseconds. The replay engine
uses only journal events received by the decision cutoff, then advances to the
simulated submit cutoff to model decision/submit latency. It never submits
exchange orders or uses future book events.
"""
import math

from src.execution.paper import PaperExecutor
from src.signals.microstructure import compute_microstructure_features


class CLOBReplayError(ValueError):
    pass


class CLOBReplayEngine:
    def __init__(self, journal, *, decision_latency_ms=250,
                 max_book_age_ms=2000, slippage_bps=8,
                 max_slippage_bps=30, default_fee_rate=0.07,
                 default_fee_exponent=1.0):
        self.journal = journal
        self.latency_ns = max(0, int(float(decision_latency_ms) * 1_000_000))
        self.max_book_age_ns = max(0, int(float(max_book_age_ms) * 1_000_000))
        self.slippage_bps = max(0.0, float(slippage_bps))
        self.max_slippage_bps = max(0.0, float(max_slippage_bps))
        self.default_fee_rate = max(0.0, float(default_fee_rate))
        self.default_fee_exponent = max(0.1, float(default_fee_exponent))

    def _book_at(self, token_id, received_before_ns):
        book = self.journal.replay_book(
            token_id, received_before_ns=int(received_before_ns)
        )
        if not book.get("complete") or not book.get("has_snapshot"):
            return None, "incomplete_book"
        latest = book.get("latest_book_update_received_ns")
        if latest is None:
            return None, "missing_book_update_timestamp"
        age_ns = max(0, int(received_before_ns) - int(latest))
        if age_ns > self.max_book_age_ns:
            return None, "stale_book"
        book["book_age_ms"] = age_ns / 1_000_000.0
        return book, None

    def replay(self, decisions):
        """Replay supplied paper intents against event-time books.

        Each intent requires token_id, market_id, decision_received_ns and
        notional_usd. Optional fields: decision_id, fee_rate, fee_exponent,
        resolution_price. A resolution price is only for offline scoring.
        """
        indexed = list(enumerate(decisions or []))
        indexed.sort(key=lambda pair: (
            int((pair[1] or {}).get("decision_received_ns", 0)), pair[0]
        ))
        previous_books = {}
        fills, rejections = [], []

        for index, intent in indexed:
            if not isinstance(intent, dict):
                rejections.append({"index": index, "reason": "invalid_decision"})
                continue
            token_id = str(intent.get("token_id") or "").strip()
            market_id = str(intent.get("market_id") or "").strip()
            try:
                decision_ns = int(intent["decision_received_ns"])
                notional = float(intent["notional_usd"])
            except (KeyError, TypeError, ValueError, OverflowError):
                rejections.append({"index": index, "reason": "invalid_decision_fields"})
                continue
            if not token_id or not market_id or decision_ns <= 0 or not math.isfinite(notional) or notional < 0.50:
                rejections.append({"index": index, "reason": "invalid_decision_fields"})
                continue

            decision_book, reason = self._book_at(token_id, decision_ns)
            if reason:
                rejections.append({
                    "index": index, "decision_id": intent.get("decision_id"),
                    "token_id": token_id, "reason": f"decision_{reason}",
                })
                continue

            features = compute_microstructure_features(
                decision_book, previous_books.get(token_id)
            )
            previous_books[token_id] = decision_book

            submit_ns = decision_ns + self.latency_ns
            submit_book, reason = self._book_at(token_id, submit_ns)
            if reason:
                rejections.append({
                    "index": index, "decision_id": intent.get("decision_id"),
                    "token_id": token_id, "reason": f"submit_{reason}",
                    "decision_book_age_ms": decision_book["book_age_ms"],
                })
                continue

            asks = submit_book.get("asks") or []
            if not asks:
                rejections.append({
                    "index": index, "decision_id": intent.get("decision_id"),
                    "token_id": token_id, "reason": "no_executable_ask",
                })
                continue
            try:
                ask = float(asks[0]["price"])
                fee_rate = float(intent.get("fee_rate", self.default_fee_rate))
                fee_exponent = float(intent.get("fee_exponent", self.default_fee_exponent))
            except (TypeError, ValueError, KeyError):
                rejections.append({
                    "index": index, "decision_id": intent.get("decision_id"),
                    "token_id": token_id, "reason": "invalid_execution_parameters",
                })
                continue

            executor = PaperExecutor(
                fee_rate=fee_rate, fee_exponent=fee_exponent,
                slippage_bps=self.slippage_bps,
                max_slippage_bps=self.max_slippage_bps,
            )
            order = executor.place_order(
                token_id, "buy", ask, notional, market_id,
                fee_rate=fee_rate, fee_exponent=fee_exponent,
                book_asks=asks,
            )
            if order is None:
                rejections.append({
                    "index": index, "decision_id": intent.get("decision_id"),
                    "token_id": token_id, "reason": "paper_execution_rejected",
                    "decision_book_age_ms": decision_book["book_age_ms"],
                    "submit_book_age_ms": submit_book["book_age_ms"],
                })
                continue

            settled = None
            if intent.get("resolution_price") is not None:
                settled = executor.resolve(order["order_id"], float(intent["resolution_price"]))
            fills.append({
                "index": index,
                "decision_id": intent.get("decision_id"),
                "fee_source": intent.get("fee_source", "unspecified"),
                "fee_verified": bool(intent.get("fee_verified", False)),
                "token_id": token_id,
                "market_id": market_id,
                "decision_received_ns": decision_ns,
                "submit_received_ns": submit_ns,
                "decision_book_age_ms": decision_book["book_age_ms"],
                "submit_book_age_ms": submit_book["book_age_ms"],
                "decision_features": features,
                "order": order,
                "settlement": settled,
            })

        realized = [
            float(row["settlement"]["pnl"])
            for row in fills if row.get("settlement") is not None
        ]
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in realized:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        wins = sum(1 for pnl in realized if pnl > 0)
        losses = sum(1 for pnl in realized if pnl < 0)
        gross_wins = sum(pnl for pnl in realized if pnl > 0)
        gross_losses = -sum(pnl for pnl in realized if pnl < 0)
        return {
            "decision_count": len(indexed),
            "filled_count": len(fills),
            "rejected_count": len(rejections),
            "resolved_count": len(realized),
            "unresolved_count": len(fills) - len(realized),
            "realized_pnl_usd": sum(realized),
            "mean_pnl_per_resolved_trade_usd": sum(realized) / len(realized) if realized else None,
            "win_rate": wins / len(realized) if realized else None,
            "profit_factor": gross_wins / gross_losses if gross_losses > 0 else None,
            "max_drawdown_usd": max_drawdown,
            "no_trade_baseline_pnl_usd": 0.0,
            "fills": fills,
            "rejections": rejections,
            "mode": "paper_event_time_replay",
        }
