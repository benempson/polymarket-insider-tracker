"""Wallet cluster detection — multiple fresh wallets trading the same market."""

from __future__ import annotations

import logging
import time
import uuid

from redis.asyncio import Redis

from polymarket_insider_tracker.detector.models import SniperClusterSignal
from polymarket_insider_tracker.ingestor.models import TradeEvent
from polymarket_insider_tracker.profiler.analyzer import WalletAnalyzer

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_MINUTES = 30
DEFAULT_MIN_WALLETS = 3
DEFAULT_MAX_NONCE = 10
DEFAULT_KEY_PREFIX = "polymarket:cluster:"


class WalletClusterDetector:
    """Detect clusters of fresh/low-activity wallets trading the same market."""

    def __init__(
        self,
        redis: Redis,
        wallet_analyzer: WalletAnalyzer,
        *,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        min_wallets: int = DEFAULT_MIN_WALLETS,
        max_nonce: int = DEFAULT_MAX_NONCE,
        key_prefix: str = DEFAULT_KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self._wallet_analyzer = wallet_analyzer
        self._window = window_minutes * 60
        self._min_wallets = min_wallets
        self._max_nonce = max_nonce
        self._prefix = key_prefix

    async def analyze(self, trade: TradeEvent) -> SniperClusterSignal | None:
        """Check if this trade forms part of a suspicious wallet cluster."""
        # Only track wallets with low activity
        try:
            profile = await self._wallet_analyzer.analyze(trade.wallet_address)
            if profile is None or profile.nonce > self._max_nonce:
                return None
        except Exception as e:
            logger.debug("Cluster detector skipping wallet %s: %s", trade.wallet_address[:10], e)
            return None

        now = time.time()
        cutoff = now - self._window
        cluster_key = f"{self._prefix}{trade.market_id}"
        alerted_key = f"{self._prefix}alerted:{trade.market_id}"

        pipe = self._redis.pipeline(transaction=False)
        pipe.zadd(cluster_key, {trade.wallet_address: now})
        pipe.zremrangebyscore(cluster_key, "-inf", cutoff)
        pipe.expire(cluster_key, self._window + 300)
        pipe.zrangebyscore(cluster_key, cutoff, "+inf")
        results = await pipe.execute()

        members = results[3]
        distinct_wallets = {(m.decode() if isinstance(m, bytes) else m) for m in members}
        cluster_size = len(distinct_wallets)

        if cluster_size < self._min_wallets:
            return None

        # Dedup: don't re-alert for the same market cluster at the same size
        dedup_member = f"{cluster_size}"
        was_new = await self._redis.sadd(alerted_key, dedup_member)
        await self._redis.expire(alerted_key, self._window + 300)
        if not was_new:
            return None

        # Confidence scales with cluster size
        confidence = min(1.0, 0.4 + (cluster_size - self._min_wallets) * 0.15)

        logger.info(
            "Wallet cluster detected: market=%s, wallets=%d, confidence=%.2f",
            trade.market_id[:10] + "...",
            cluster_size,
            confidence,
        )

        return SniperClusterSignal(
            wallet_address=trade.wallet_address,
            cluster_id=str(uuid.uuid4()),
            cluster_size=cluster_size,
            avg_entry_delta_seconds=float(self._window),
            markets_in_common=1,
            confidence=confidence,
        )
