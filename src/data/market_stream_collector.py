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

from src.data.market_event_journal import MarketEventJournal

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

    async def _heartbeat(self, websocket):
        while True:
            await asyncio.sleep(10)
            await websocket.send("PING")

    async def run_forever(self):
        backoff = self.base_backoff_seconds
        while not self.stop_event.is_set():
            heartbeat = None
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, close_timeout=5, max_queue=2048
                ) as websocket:
                    await websocket.send(json.dumps({
                        "assets_ids": self.token_ids,
                        "type": "market",
                        "custom_feature_enabled": self.custom_features,
                    }))
                    logger.info("CLOB market stream connected; tokens=%d", len(self.token_ids))
                    heartbeat = asyncio.create_task(self._heartbeat(websocket))
                    async for raw in websocket:
                        try:
                            events = _decode_payload(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            logger.warning("Skipping malformed WebSocket frame: %s", exc)
                            continue
                        for event in events:
                            try:
                                self.journal.append(event)
                            except Exception as exc:
                                logger.warning("Skipping malformed market event: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("CLOB market stream disconnected: %s", exc)
            finally:
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


async def _main(args):
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    journal = MarketEventJournal(args.db)
    collector = MarketStreamCollector(
        args.tokens.split(","),
        journal,
        custom_features=not args.no_custom_features,
        base_backoff_seconds=args.base_backoff,
        max_backoff_seconds=args.max_backoff,
    )
    try:
        await collector.run_forever()
    except KeyboardInterrupt:
        collector.stop()


def main():
    parser = argparse.ArgumentParser(description="Collect public Polymarket CLOB events to a local paper-data journal")
    parser.add_argument("--tokens", required=True, help="Comma-separated outcome token IDs")
    parser.add_argument("--db", default="data/clob_events.sqlite", help="SQLite journal path")
    parser.add_argument("--no-custom-features", action="store_true")
    parser.add_argument("--base-backoff", type=float, default=1.0)
    parser.add_argument("--max-backoff", type=float, default=30.0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
