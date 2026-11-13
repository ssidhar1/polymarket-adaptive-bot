"""
Lightweight JSON state so restarts don't wipe bankroll / learner threshold.
"""
import json
import math
import os
from pathlib import Path
from typing import Any, Dict
from datetime import datetime, timezone
import logging

logger = logging.getLogger("astf.state")
DEFAULT_PATH = Path("runtime_state.json")


def _finite_num(x, default=None):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


class StateStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("State file is not a dict — ignoring")
                return {}
            # Sanitize numeric fields
            if "bankroll" in data:
                b = _finite_num(data["bankroll"])
                if b is None or b < 0:
                    data.pop("bankroll", None)
                else:
                    data["bankroll"] = b
            if "peak_bankroll" in data:
                p = _finite_num(data["peak_bankroll"])
                if p is None or p < 0:
                    data.pop("peak_bankroll", None)
                else:
                    data["peak_bankroll"] = p
            if "open_positions" in data and not isinstance(data["open_positions"], list):
                data["open_positions"] = []
            return data
        except Exception as e:
            logger.warning(f"Could not load state: {e}")
            return {}

    def save(self, data: Dict[str, Any]):
        payload = {
            **data,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")
            try:
                tmp = self.path.with_suffix(".tmp")
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
