"""Rapid multi-market activity detection."""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis

from polymarket_insider_tracker.detector.models import MultiMarketSignal
from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_MINUTES = 60
DEFAULT_MIN_MARKETS = 5
DEFAULT_KEY_PREFIX = "polymarket:multimarket:"


class MultiMarketDetector:
    """Detect wallets trading many distinct markets in a short window.

    Normal traders focus on 1-2 markets.  A wallet placing trades
    across 5+ markets within an hour suggests coordinated activity
    or someone acting on broad non-public information.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        min_markets: int = DEFAULT_MIN_MARKETS,
        key_prefix: str = DEFAULT_KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self._window = window_minutes * 60
        self._min_markets = min_markets
        self._prefix = key_prefix
        self._window_minutes = window_minutes

    async def analyze(self, trade: TradeEvent) -> MultiMarketSignal | None:
        now = time.time()
        cutoff = now - self._window
        wallet_key = f"{self._prefix}{trade.wallet_address}"

        pipe = self._redis.pipeline(transaction=False)
        pipe.zadd(wallet_key, {trade.market_id: now})
        pipe.zremrangebyscore(wallet_key, "-inf", cutoff)
        pipe.expire(wallet_key, self._window + 300)
        pipe.zrangebyscore(wallet_key, cutoff, "+inf")
        results = await pipe.execute()

        members = results[3]
        distinct_markets = {(m.decode() if isinstance(m, bytes) else m) for m in members}
        markets_traded = len(distinct_markets)

        if markets_traded < self._min_markets:
            return None

        # Confidence scales with number of markets
        confidence = min(1.0, 0.4 + (markets_traded - self._min_markets) * 0.12)

        factors = {
            "markets_traded": float(markets_traded),
            "window_minutes": float(self._window_minutes),
        }

        logger.info(
            "Multi-market signal: wallet=%s, markets=%d, confidence=%.2f",
            trade.wallet_address[:10] + "...",
            markets_traded,
            confidence,
        )

        return MultiMarketSignal(
            trade_event=trade,
            wallet_address=trade.wallet_address,
            markets_traded=markets_traded,
            window_minutes=self._window_minutes,
            confidence=confidence,
            factors=factors,
        )
