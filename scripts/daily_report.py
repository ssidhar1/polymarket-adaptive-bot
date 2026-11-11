"""
Print daily + all-time trade summary from trades.db
Usage: python -m scripts.daily_report
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.persistence import TradeLogger
from src.utils.logging import setup_logging

setup_logging("INFO")


def main():
    log = TradeLogger()
    d = log.daily_summary()
    a = log.all_time_summary()
    recent = log.recent(5)

    print("\n=== ASTF Daily Report ===")
    print(f"Trades today : {d['trades']}")
    print(f"Wins         : {d['wins']}")
    print(f"Win rate     : {d['win_rate']:.1%}")
    print(f"PnL today    : ${d['pnl']:.2f}")
    print("\n--- All time ---")
    print(f"Trades       : {a['trades']}")
    print(f"Win rate     : {a['win_rate']:.1%}")
    print(f"Total PnL    : ${a['pnl']:.2f}")
    print(f"Avg score    : {a['avg_score']:.3f}")
    if recent:
        print("\n--- Recent ---")
        for t in recent:
            print(
                f"  {t.get('direction','?'):4} pnl=${float(t.get('pnl') or 0):+.2f}  "
                f"{str(t.get('slug') or '')[:36]}"
            )
    print("========================\n")


if __name__ == "__main__":
    main()
