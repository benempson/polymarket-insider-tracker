"""Whale / smart-money tracking — alert on high-volume wallet activity."""

from __future__ import annotations

import logging
from decimal import Decimal

from redis.asyncio import Redis

from polymarket_insider_tracker.detector.models import WhaleSignal
from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)

DEFAULT_WHALE_VOLUME_THRESHOLD = Decimal("50000")  # $50k total volume
DEFAULT_WHALE_MIN_TRADES = 10
DEFAULT_KEY_PREFIX = "polymarket:whale:"
DEFAULT_MARKET_TTL = 86_400  # 24h window for "new market" check


class WhaleTracker:
    """Track high-volume wallets and alert when they enter new markets.

    A wallet is considered a "whale" when its cumulative trading volume
    and trade count exceed configurable thresholds.  When a whale trades
    a market for the first time, a signal is emitted.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        volume_threshold: Decimal = DEFAULT_WHALE_VOLUME_THRESHOLD,
        min_trades: int = DEFAULT_WHALE_MIN_TRADES,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        market_ttl: int = DEFAULT_MARKET_TTL,
    ) -> None:
        self._redis = redis
        self._volume_threshold = volume_threshold
        self._min_trades = min_trades
        self._prefix = key_prefix
        self._market_ttl = market_ttl

    async def analyze(self, trade: TradeEvent) -> WhaleSignal | None:
        wallet_key = f"{self._prefix}{trade.wallet_address}"
        markets_key = f"{self._prefix}{trade.wallet_address}:markets"
        notional = float(trade.notional_value)

        # Update cumulative stats
        pipe = self._redis.pipeline(transaction=False)
        pipe.hincrbyfloat(wallet_key, "total_volume", notional)
        pipe.hincrby(wallet_key, "trade_count", 1)
        pipe.expire(wallet_key, self._market_ttl * 30)  # keep whale stats ~30 days
        results = await pipe.execute()

        total_volume = Decimal(str(results[0]))
        trade_count = int(results[1])

        # Is this wallet a whale?
        if total_volume < self._volume_threshold or trade_count < self._min_trades:
            return None

        # Is this a new market for this whale?
        was_new = await self._redis.sadd(markets_key, trade.market_id)
        await self._redis.expire(markets_key, self._market_ttl * 30)

        if not was_new:
            return None

        # Count total markets
        markets_count = await self._redis.scard(markets_key)

        confidence = min(1.0, 0.3 + float(total_volume / (self._volume_threshold * 10)) * 0.4)
        confidence = max(0.0, min(1.0, confidence))

        factors = {
            "total_volume": float(total_volume),
            "trade_count": float(trade_count),
            "markets_count": float(markets_count),
        }

        logger.info(
            "Whale signal: wallet=%s, volume=$%.0f, trades=%d, new market=%s",
            trade.wallet_address[:10] + "...",
            total_volume,
            trade_count,
            trade.market_id[:10] + "...",
        )

        return WhaleSignal(
            trade_event=trade,
            wallet_total_volume=total_volume,
            wallet_trade_count=trade_count,
            wallet_markets_count=int(markets_count),
            confidence=confidence,
            factors=factors,
        )
