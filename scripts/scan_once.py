"""
Quick utility: scan active short-term crypto markets + book snapshot.
Usage: python -m scripts.scan_once
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.polymarket_client import PolymarketData
from src.utils.logging import setup_logging

setup_logging("INFO")


def main():
    data = PolymarketData()
    markets = data.get_active_short_term_markets(["btc", "eth"])
    print(f"\nFound {len(markets)} markets:\n")
    for m in markets:
        slug = m.get("slug", "?")
        q = (m.get("question") or "")[:70]
        up = m.get("_up_token")
        down = m.get("_down_token")
        book_up = data.get_order_book(up) if up else {}
        book_down = data.get_order_book(down) if down else {}
        up_mid = book_up.get("mid")
        down_mid = book_down.get("mid")
        total = None
        if up_mid is not None and down_mid is not None:
            total = up_mid + down_mid
        print(f"  {slug}")
        print(f"    {q}")
        print(
            f"    up_mid={up_mid}  down_mid={down_mid}  "
            f"sum={total if total is not None else 'n/a'}"
        )
        if total is not None and total < 0.98:
            print(f"    *** possible complete-set edge: {1 - total:.3f} before fees")
        print()


if __name__ == "__main__":
    main()
