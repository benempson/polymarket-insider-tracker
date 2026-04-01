"""Rolling market statistics aggregated from the live trade stream.

Maintains per-market 24h volume, trade count, unique traders, and
median trade size in Redis.  Designed for high-throughput writes
(called on every trade) and fast reads by downstream detectors.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SECONDS = 86_400  # 24 hours
DEFAULT_KEY_PREFIX = "polymarket:mstats:"
DEFAULT_MAX_SIZE_ENTRIES = 2000  # cap sorted set for median calculation


@dataclass(frozen=True)
class MarketStats:
    """Rolling statistics for a single market."""

    market_id: str
    volume_24h: Decimal
    trade_count_24h: int
    unique_traders_24h: int
    median_trade_size: Decimal | None


class MarketStatsAggregator:
    """Aggregate per-market statistics from the trade stream into Redis."""

    def __init__(
        self,
        redis: Redis,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        max_size_entries: int = DEFAULT_MAX_SIZE_ENTRIES,
    ) -> None:
        self._redis = redis
        self._window = window_seconds
        self._prefix = key_prefix
        self._max_size_entries = max_size_entries

    # -- write path (called on every trade) --------------------------------

    async def record_trade(
        self,
        market_id: str,
        wallet_address: str,
        notional_value: Decimal,
        trade_id: str,
    ) -> None:
        """Record a single trade into the rolling window."""
        now = time.time()
        cutoff = now - self._window

        trades_key = f"{self._prefix}{market_id}:trades"
        traders_key = f"{self._prefix}{market_id}:traders"
        sizes_key = f"{self._prefix}{market_id}:sizes"

        pipe = self._redis.pipeline(transaction=False)

        # Sorted set of trades: score=timestamp, member=trade_id:notional
        member = f"{trade_id}:{notional_value}"
        pipe.zadd(trades_key, {member: now})
        pipe.zremrangebyscore(trades_key, "-inf", cutoff)
        pipe.expire(trades_key, self._window + 3600)

        # Sorted set of traders: score=timestamp, member=wallet
        # We always update the score so the latest timestamp wins for dedup.
        pipe.zadd(traders_key, {wallet_address: now})
        pipe.zremrangebyscore(traders_key, "-inf", cutoff)
        pipe.expire(traders_key, self._window + 3600)

        # Sorted set of recent trade sizes for median (capped)
        pipe.zadd(sizes_key, {member: float(notional_value)})
        pipe.zremrangebyrank(sizes_key, 0, -(self._max_size_entries + 1))
        pipe.expire(sizes_key, self._window + 3600)

        await pipe.execute()

    # -- read path (called by detectors) -----------------------------------

    async def get_stats(self, market_id: str) -> MarketStats | None:
        """Retrieve rolling stats for a market.  Returns None if no data."""
        trades_key = f"{self._prefix}{market_id}:trades"
        traders_key = f"{self._prefix}{market_id}:traders"
        sizes_key = f"{self._prefix}{market_id}:sizes"

        now = time.time()
        cutoff = now - self._window

        pipe = self._redis.pipeline(transaction=False)
        pipe.zrangebyscore(trades_key, cutoff, "+inf")
        pipe.zrangebyscore(traders_key, cutoff, "+inf")
        pipe.zrangebyscore(sizes_key, "-inf", "+inf", withscores=True)
        results = await pipe.execute()

        trade_members: list[bytes | str] = results[0]
        trader_members: list[bytes | str] = results[1]
        size_pairs: list[tuple[bytes | str, float]] = results[2]

        if not trade_members:
            return None

        # Sum volume from trade members ("trade_id:notional")
        volume = Decimal(0)
        for m in trade_members:
            raw = m.decode() if isinstance(m, bytes) else m
            parts = raw.rsplit(":", 1)
            if len(parts) == 2:
                with contextlib.suppress(Exception):
                    volume += Decimal(parts[1])

        # Unique traders
        unique_traders = len(trader_members)

        # Median from sizes sorted set (scores are the notional values)
        median = None
        if size_pairs:
            sorted_sizes = sorted(s for _, s in size_pairs)
            mid = len(sorted_sizes) // 2
            if len(sorted_sizes) % 2 == 0 and len(sorted_sizes) >= 2:
                median = Decimal(str((sorted_sizes[mid - 1] + sorted_sizes[mid]) / 2))
            else:
                median = Decimal(str(sorted_sizes[mid]))

        return MarketStats(
            market_id=market_id,
            volume_24h=volume,
            trade_count_24h=len(trade_members),
            unique_traders_24h=unique_traders,
            median_trade_size=median,
        )
