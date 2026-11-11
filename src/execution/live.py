"""
Live execution skeleton.
Requires PRIVATE_KEY in .env and careful testing.
Do not enable until paper results are consistently positive.
"""
import os
import logging
from typing import Optional, Dict

logger = logging.getLogger("astf.live")

class LiveExecutor:
    def __init__(self):
        self.client = None
        self.ready = False
        try:
            from polymarket import SecureClient  # official unified SDK
            pk = os.getenv("PRIVATE_KEY")
            if not pk:
                logger.warning("No PRIVATE_KEY set — live disabled")
                return
            # Exact init depends on current SDK version — check docs
            # self.client = SecureClient(...)
            logger.info("LiveExecutor skeleton loaded (not fully wired)")
            self.ready = False  # keep False until explicitly completed
        except Exception as e:
            logger.warning(f"Could not init live client: {e}")

    def place_order(self, token_id: str, side: str, price: float, size_usd: float, market_id: str) -> Optional[Dict]:
        if not self.ready or not self.client:
            logger.error("Live trading not enabled")
            return None
        # TODO: sign + post limit order via official client
        # return {"order_id": ..., "status": "live", ...}
        raise NotImplementedError("Complete live order placement after paper validation")
