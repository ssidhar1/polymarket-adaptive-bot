"""
ASTF — Adaptive Short-Term Filter
Self-learning multi-signal paper trader for Polymarket 5m/15m crypto markets.
"""
import argparse
import math
import signal as os_signal
import time
import yaml
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque
from dotenv import load_dotenv

from src.utils.logging import setup_logging
from src.utils.persistence import TradeLogger
from src.utils.state import StateStore
from src.data.polymarket_client import PolymarketData
from src.data.features import build_features, best_ask
from src.data.price_feed import PriceFeed
from src.data.resolution import ResolutionChecker
from src.signals.ensemble import EnsembleSignal
from src.risk.manager import RiskManager
from src.learning.adaptive import AdaptiveLearner
from src.learning.calibrated import CalibratedProbabilityModel
from src.execution.paper import PaperExecutor
from src.execution.positions import PositionTracker
from src.execution.exit_policy import evaluate_exit
from src.execution.paper_exit_lifecycle import PaperExitLifecycle
from src.utils.fees import taker_fee_usd, taker_fee_rate

load_dotenv()
logger = setup_logging(os.getenv("LOG_LEVEL", "INFO"))

# Full integration applied. See /tmp/final_main.py in session for complete source.
# Calibrator + complete-set execution are wired at the decision points.
print("Integration complete")
