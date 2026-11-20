"""Optional paper-data collector for Polymarket's public CLOB market channel.

Run separately from the trading loop. It never authenticates, submits orders,
or accesses the private user channel.
"""
import argparse
import asyncio
import json
import logging
import time

import websockets

from src.data.market_event_journal import MarketEventError, MarketEventJournal
from src.data.polymarket_client import PolymarketData

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
logger = logging.getLogger("astf.market_stream")


def _decode_payload(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if raw in ("PONG", "PING", ""):
        return []
    value = json.loads(raw)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


class MarketStreamCollector:
    def __init__(self, token_ids, journal, *, custom_features=True,
                 base_backoff_seconds=1.0, max_backoff_seconds=30.0):
        self.token_ids = sorted({str(token).strip() for token in token_ids if str(token).strip()})
        if not self.token_ids:
            raise ValueError("at least one CLOB token ID is required")
        self.journal = journal
        self.custom_features = bool(custom_features)
        self.base_backoff_seconds = max(0.25, float(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, float(max_backoff_seconds))
        self.stop_event = asyncio.Event()
        self.websocket = None

    async def update_tokens(self, token_ids):
        """Update subscriptions without reconnecting; invalidate removed-token books."""
        updated = sorted({str(token).strip() for token in token_ids if str(token).strip()})
        if not updated:
            return {"added": [], "removed": []}
        old, new = set(self.token_ids), set(updated)
        added, removed = sorted(new - old), sorted(old - new)
        self.token_ids = updated
        if removed:
            self.journal.record_connection_gap(
                removed, reason="token unsubscribed; no longer receiving live updates"
            )
        ws = self.websocket
        if ws is not None:
            if added:
                await ws.send(json.dumps({"assets_ids": added, "operation": "subscribe"}))
            if removed:
                await ws.send(json.dumps({"assets_ids": removed, "operation": "unsubscribe"}))
        return {"added": added, "removed": removed}

    async def _heartbeat(self, websocket):
        while True:
            await asyncio.sleep(10)
            await websocket.send("PING")

    async def run_forever(self):
        backoff = self.base_backoff_seconds
        while not self.stop_event.is_set():
            heartbeat = None
            connected_at = None
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, close_timeout=5, max_queue=2048
                ) as websocket:
                    await websocket.send(json.dumps({
                        "assets_ids": self.token_ids,
                        "type": "market",
                        "custom_feature_enabled": self.custom_features,
                    }))
                    self.websocket = websocket
                    logger.info("CLOB market stream connected; tokens=%d", len(self.token_ids))
                    connected_at = time.monotonic()
                    heartbeat = asyncio.create_task(self._heartbeat(websocket))
                    while not self.stop_event.is_set():
                        # A live TCP socket is not proof that the application
                        # heartbeat is healthy. Force reconnect if PONG/market
                        # frames stop arriving despite our 10-second PINGs.
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=25)
                        except asyncio.TimeoutError as exc:
                            raise ConnectionError("no WebSocket frame/PONG received within 25 seconds") from exc
                        try:
                            events = _decode_payload(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            logger.warning("Skipping malformed WebSocket frame: %s", exc)
                            continue
                        for event in events:
                            try:
                                self.journal.append(event)
                            except MarketEventError as exc:
                                logger.warning("Skipping malformed market event: %s", exc)
                            except Exception as exc:
                                # Storage errors must not be swallowed as bad
                                # input: force a reconnect and preserve a gap.
                                logger.error("CLOB journal write failed: %s", exc)
                                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("CLOB market stream disconnected: %s", exc)
            finally:
                self.websocket = None
                if connected_at is not None and time.monotonic() - connected_at >= 60:
                    backoff = self.base_backoff_seconds
                if heartbeat is not None:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except (asyncio.CancelledError, Exception):
                        pass
                # Persist a gap even when a reconnect attempt fails before the
                # socket opens: an older on-disk snapshot must not masquerade
                # as current while the collector is offline.
                try:
                    self.journal.record_connection_gap(
                        self.token_ids,
                        reason="market websocket unavailable/disconnected; waiting for fresh snapshots",
                    )
                except Exception as exc:
                    logger.error("Could not persist connection gap: %s", exc)
            if self.stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(self.max_backoff_seconds, backoff * 2.0)

    def stop(self):
        self.stop_event.set()


async def _discover_tokens(data):
    markets = await asyncio.to_thread(data.get_active_short_term_markets)
    tokens = set()
    for market in markets or []:
        for key in ("_up_token", "_down_token"):
            token = str(market.get(key) or "").strip()
            if token:
                tokens.add(token)
    return sorted(tokens)


async def _refresh_discovered_tokens(collector, data, interval):
    while not collector.stop_event.is_set():
        try:
            tokens = await _discover_tokens(data)
            if tokens:
                changes = await collector.update_tokens(tokens)
                if changes["added"] or changes["removed"]:
                    logger.info("CLOB subscriptions updated: +%d -%d",
                                len(changes["added"]), len(changes["removed"]))
        except Exception as exc:
            logger.warning("Market discovery/subscription refresh failed: %s", exc)
        try:
            await asyncio.wait_for(collector.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _main(args):
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    journal = MarketEventJournal(args.db)
    data = PolymarketData() if args.discover_short_term else None
    tokens = [token.strip() for token in (args.tokens or "").split(",") if token.strip()]
    if data is not None:
        discovered = await _discover_tokens(data)
        tokens = sorted(set(tokens) | set(discovered))
    if not tokens:
        raise SystemExit("Provide --tokens or use --discover-short-term when markets are active")
    collector = MarketStreamCollector(
        tokens,
        journal,
        custom_features=not args.no_custom_features,
        base_backoff_seconds=args.base_backoff,
        max_backoff_seconds=args.max_backoff,
    )
    tasks = [asyncio.create_task(collector.run_forever())]
    if data is not None:
        tasks.append(asyncio.create_task(
            _refresh_discovered_tokens(collector, data, args.discovery_interval)
        ))
    try:
        await asyncio.gather(*tasks)
    finally:
        collector.stop()
        for task in tasks:
            if not task.done():
                task.cancel()


def main():
    parser = argparse.ArgumentParser(description="Collect public Polymarket CLOB events to a local paper-data journal")
    parser.add_argument("--tokens", default="", help="Comma-separated outcome token IDs")
    parser.add_argument("--discover-short-term", action="store_true",
                        help="Automatically subscribe to active BTC/ETH 5m and 15m markets")
    parser.add_argument("--discovery-interval", type=float, default=30.0)
    parser.add_argument("--db", default="data/clob_events.sqlite", help="SQLite journal path")
    parser.add_argument("--no-custom-features", action="store_true")
    parser.add_argument("--base-backoff", type=float, default=1.0)
    parser.add_argument("--max-backoff", type=float, default=30.0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
