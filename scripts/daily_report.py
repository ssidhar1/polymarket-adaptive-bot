"""
Print daily trade summary from trades.csv
Usage: python -m scripts.daily_report
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.persistence import TradeLogger
from src.utils.logging import setup_logging

logger = setup_logging("INFO")

def main():
    log = TradeLogger()
    s = log.daily_summary()
    print("\n=== ASTF Daily Report ===")
    print(f"Trades today : {s['trades']}")
    print(f"Wins         : {s['wins']}")
    print(f"Win rate     : {s['win_rate']:.1%}")
    print(f"PnL today    : ${s['pnl']:.2f}")
    print("========================\n")

if __name__ == "__main__":
    main()
