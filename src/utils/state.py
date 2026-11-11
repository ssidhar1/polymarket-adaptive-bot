"""
Lightweight JSON state so restarts don't wipe bankroll / learner threshold.
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger("astf.state")
DEFAULT_PATH = Path("runtime_state.json")


class StateStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load state: {e}")
            return {}

    def save(self, data: Dict[str, Any]):
        payload = {
            **data,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            tmp.replace(self.path)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")
