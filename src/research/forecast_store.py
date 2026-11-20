"""SQLite event-time forecast store with separate, post-observation resolution events."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.research.walk_forward import ForecastOutcome


class ForecastStoreError(ValueError):
    pass


def _utc(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ForecastStoreError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


class ForecastStore:
    """Append forecast snapshots first; attach outcome labels only after resolution."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS forecast_observations (
                forecast_id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                market_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                resolution_at TEXT NOT NULL,
                probability_yes REAL,
                entry_price REAL NOT NULL,
                fees_per_share REAL NOT NULL,
                slippage_per_share REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS forecast_resolution_events (
                event_id TEXT PRIMARY KEY,
                forecast_id TEXT NOT NULL UNIQUE
                    REFERENCES forecast_observations(forecast_id),
                outcome_yes INTEGER NOT NULL CHECK(outcome_yes IN (0,1)),
                resolved_at TEXT NOT NULL,
                resolution_source TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_forecast_strategy_time ON forecast_observations(strategy, observed_at)")

    def record_observation(
        self, *, forecast_id: str, strategy: str, market_id: str,
        observed_at: datetime, resolution_at: datetime,
        probability_yes: float | None, entry_price: float,
        fees_per_share: float, slippage_per_share: float,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        observed = _utc(observed_at, "observed_at")
        resolution = _utc(resolution_at, "resolution_at")
        if not forecast_id or not strategy or not market_id or observed >= resolution:
            raise ForecastStoreError("forecast identity is required and observation must precede resolution")
        try:
            entry = float(entry_price)
            fees = float(fees_per_share)
            slippage = float(slippage_per_share)
            probability = None if probability_yes is None else float(probability_yes)
        except (TypeError, ValueError, OverflowError):
            raise ForecastStoreError("forecast numeric inputs must be valid numbers") from None
        numeric = [entry, fees, slippage] + ([] if probability is None else [probability])
        if any(not math.isfinite(value) for value in numeric):
            raise ForecastStoreError("forecast numeric inputs must be finite")
        if probability is not None and not 0 <= probability <= 1:
            raise ForecastStoreError("probability_yes must be between zero and one")
        if not 0 < entry < 1:
            raise ForecastStoreError("entry_price must be between zero and one")
        if fees < 0 or slippage < 0:
            raise ForecastStoreError("cost estimates cannot be negative")
        payload = (
            str(forecast_id), str(strategy), str(market_id), observed, resolution,
            probability,
            entry, fees, slippage,
            json.dumps(metadata or {}, sort_keys=True),
        )
        with self._connect() as db:
            prior = db.execute(
                "SELECT strategy,market_id,observed_at,resolution_at,probability_yes,entry_price,fees_per_share,slippage_per_share,metadata_json FROM forecast_observations WHERE forecast_id=?",
                (str(forecast_id),),
            ).fetchone()
            if prior:
                existing = tuple(prior)
                if existing != payload[1:]:
                    raise ForecastStoreError("forecast_id reused with different observation data")
                return str(forecast_id)
            db.execute("""INSERT INTO forecast_observations
                (forecast_id,strategy,market_id,observed_at,resolution_at,probability_yes,
                 entry_price,fees_per_share,slippage_per_share,metadata_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (*payload, datetime.now(timezone.utc).isoformat()))
        return str(forecast_id)

    def record_resolution(
        self, *, forecast_id: str, event_id: str, outcome_yes: bool,
        resolved_at: datetime, resolution_source: str,
    ) -> None:
        stamp = _utc(resolved_at, "resolved_at")
        if not event_id or not resolution_source or not isinstance(outcome_yes, bool):
            raise ForecastStoreError("resolution event id, boolean outcome and source are required")
        with self._connect() as db:
            row = db.execute(
                "SELECT observed_at FROM forecast_observations WHERE forecast_id=?",
                (str(forecast_id),),
            ).fetchone()
            if not row:
                raise ForecastStoreError("unknown forecast_id")
            if stamp <= row["observed_at"]:
                raise ForecastStoreError("resolution event must occur after the forecast observation")
            prior = db.execute(
                "SELECT forecast_id,outcome_yes,resolved_at,resolution_source FROM forecast_resolution_events WHERE event_id=?",
                (str(event_id),),
            ).fetchone()
            if prior:
                expected = (str(forecast_id), int(outcome_yes), stamp, str(resolution_source))
                if tuple(prior) != expected:
                    raise ForecastStoreError("resolution event_id reused with different data")
                return
            existing = db.execute(
                "SELECT event_id FROM forecast_resolution_events WHERE forecast_id=?",
                (str(forecast_id),),
            ).fetchone()
            if existing:
                raise ForecastStoreError("forecast already has an authoritative resolution event")
            db.execute("""INSERT INTO forecast_resolution_events
                (event_id,forecast_id,outcome_yes,resolved_at,resolution_source)
                VALUES(?,?,?,?,?)""",
                (str(event_id), str(forecast_id), int(outcome_yes), stamp, str(resolution_source)))

    def load_resolved(self, strategy: str | None = None) -> list[ForecastOutcome]:
        query = """SELECT f.*,r.outcome_yes FROM forecast_observations f
            JOIN forecast_resolution_events r ON r.forecast_id=f.forecast_id
            WHERE f.probability_yes IS NOT NULL"""
        params: tuple = ()
        if strategy:
            query += " AND f.strategy=?"
            params = (strategy,)
        query += " ORDER BY f.observed_at"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [
            ForecastOutcome(
                strategy=row["strategy"], market_id=row["market_id"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                resolution_at=datetime.fromisoformat(row["resolution_at"]),
                probability_yes=float(row["probability_yes"]),
                entry_price=float(row["entry_price"]),
                outcome_yes=bool(row["outcome_yes"]),
                fees_per_share=float(row["fees_per_share"]),
                slippage_per_share=float(row["slippage_per_share"]),
            )
            for row in rows
        ]

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            observations = db.execute("SELECT COUNT(*) FROM forecast_observations").fetchone()[0]
            resolved = db.execute("SELECT COUNT(*) FROM forecast_resolution_events").fetchone()[0]
            modeled = db.execute("SELECT COUNT(*) FROM forecast_observations WHERE probability_yes IS NOT NULL").fetchone()[0]
        return {"observations": int(observations), "resolved": int(resolved), "modeled_observations": int(modeled)}
