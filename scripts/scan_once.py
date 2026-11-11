"""
Quick utility: scan and print currently active short-term crypto markets.
Usage: python -m scripts.scan_once
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.polymarket_client import PolymarketData
from src.utils.logging import setup_logging

logger = setup_logging("INFO")

def main():
    data = PolymarketData()
    markets = data.get_active_short_term_markets(["btc", "eth"])
    print(f"\nFound {len(markets)} markets:\n")
    for m in markets:
        slug = m.get("slug", "?")
        q = m.get("question", "")[:70]
        print(f"  {slug}")
        print(f"    {q}")
        print(f"    up={m.get('_up_token', '')[:16]}...  down={m.get('_down_token', '')[:16]}...")
        print()

if __name__ == "__main__":
    main()
