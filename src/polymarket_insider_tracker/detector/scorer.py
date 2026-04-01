"""Composite risk scorer combining all detector signals.

This module provides the RiskScorer class that aggregates signals from
multiple detectors into a unified risk assessment with weighted scoring.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from polymarket_insider_tracker.detector.models import (
    ConvictionSignal,
    FreshWalletSignal,
    MultiMarketSignal,
    RiskAssessment,
    SizeAnomalySignal,
    SniperClusterSignal,
    TimingSignal,
    WhaleSignal,
)
from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_INFO_THRESHOLD = 0.4
DEFAULT_HIGH_THRESHOLD = 0.6
DEFAULT_DEDUP_WINDOW_SECONDS = 3600  # 1 hour
DEFAULT_REDIS_KEY_PREFIX = "polymarket:dedup:"

# Default weights for each signal type
DEFAULT_WEIGHTS: dict[str, float] = {
    "fresh_wallet": 0.35,
    "size_anomaly": 0.25,
    "niche_market": 0.15,
    "sniper_cluster": 0.25,
    "conviction": 0.20,
    "timing": 0.20,
    "multi_market": 0.15,
    "whale": 0.15,
}

# Multi-signal bonuses
MULTI_SIGNAL_BONUS_2 = 1.15
MULTI_SIGNAL_BONUS_3 = 1.25
MULTI_SIGNAL_BONUS_4 = 1.35

# If any single signal has confidence >= this, floor the score at info threshold
HIGH_CONFIDENCE_SINGLE_SIGNAL = 0.8


@dataclass
class SignalBundle:
    """Bundle of signals for a single trade."""

    trade_event: TradeEvent
    fresh_wallet_signal: FreshWalletSignal | None = None
    size_anomaly_signal: SizeAnomalySignal | None = None
    sniper_cluster_signal: SniperClusterSignal | None = None
    conviction_signal: ConvictionSignal | None = None
    timing_signal: TimingSignal | None = None
    multi_market_signal: MultiMarketSignal | None = None
    whale_signal: WhaleSignal | None = None

    @property
    def wallet_address(self) -> str:
        return self.trade_event.wallet_address

    @property
    def market_id(self) -> str:
        return self.trade_event.market_id


class RiskScorer:
    """Composite risk scorer combining signals into unified assessments.

    Two-tier alerting:
      - info tier (>= 0.4): informational alert for single strong signals
      - high tier (>= 0.6): high-confidence alert requiring corroboration
    """

    def __init__(
        self,
        redis: Redis,
        *,
        weights: dict[str, float] | None = None,
        info_threshold: float = DEFAULT_INFO_THRESHOLD,
        high_threshold: float = DEFAULT_HIGH_THRESHOLD,
        dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        key_prefix: str = DEFAULT_REDIS_KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self._weights = weights or DEFAULT_WEIGHTS.copy()
        self._info_threshold = info_threshold
        self._high_threshold = high_threshold
        self._dedup_window = dedup_window_seconds
        self._key_prefix = key_prefix

    async def assess(self, bundle: SignalBundle) -> RiskAssessment:
        """Assess a trade's risk based on all available signals."""
        weighted_score, signals_triggered = self.calculate_weighted_score(bundle)

        # Determine alert tier
        if weighted_score >= self._high_threshold:
            alert_tier = "high"
        elif weighted_score >= self._info_threshold:
            alert_tier = "info"
        else:
            alert_tier = "none"

        meets_threshold = alert_tier != "none"

        # Dedup check
        is_duplicate = False
        if meets_threshold:
            is_duplicate = await self._check_and_set_dedup(
                bundle.wallet_address,
                bundle.market_id,
            )

        should_alert = meets_threshold and not is_duplicate

        if should_alert:
            logger.info(
                "Risk assessment triggered alert: wallet=%s, market=%s, score=%.2f, "
                "signals=%d, tier=%s",
                bundle.wallet_address[:10] + "...",
                bundle.market_id[:10] + "...",
                weighted_score,
                signals_triggered,
                alert_tier,
            )
        elif is_duplicate:
            logger.debug(
                "Risk assessment deduplicated: wallet=%s, market=%s",
                bundle.wallet_address[:10] + "...",
                bundle.market_id[:10] + "...",
            )

        return RiskAssessment(
            trade_event=bundle.trade_event,
            wallet_address=bundle.wallet_address,
            market_id=bundle.market_id,
            fresh_wallet_signal=bundle.fresh_wallet_signal,
            size_anomaly_signal=bundle.size_anomaly_signal,
            sniper_cluster_signal=bundle.sniper_cluster_signal,
            conviction_signal=bundle.conviction_signal,
            timing_signal=bundle.timing_signal,
            multi_market_signal=bundle.multi_market_signal,
            whale_signal=bundle.whale_signal,
            signals_triggered=signals_triggered,
            weighted_score=weighted_score,
            should_alert=should_alert,
            alert_tier=alert_tier,
        )

    def calculate_weighted_score(self, bundle: SignalBundle) -> tuple[float, int]:
        """Calculate weighted score from all signals."""
        score = 0.0
        signals_triggered = 0
        max_single_confidence = 0.0

        # Fresh wallet signal
        if bundle.fresh_wallet_signal is not None:
            w = self._weights.get("fresh_wallet", 0.0)
            score += bundle.fresh_wallet_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.fresh_wallet_signal.confidence)

        # Size anomaly signal
        if bundle.size_anomaly_signal is not None:
            w = self._weights.get("size_anomaly", 0.0)
            score += bundle.size_anomaly_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.size_anomaly_signal.confidence)

            # Niche market bonus
            if bundle.size_anomaly_signal.is_niche_market:
                nw = self._weights.get("niche_market", 0.0)
                score += bundle.size_anomaly_signal.confidence * nw

        # Sniper cluster signal
        if bundle.sniper_cluster_signal is not None:
            w = self._weights.get("sniper_cluster", 0.0)
            score += bundle.sniper_cluster_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.sniper_cluster_signal.confidence)

        # Conviction signal
        if bundle.conviction_signal is not None:
            w = self._weights.get("conviction", 0.0)
            score += bundle.conviction_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.conviction_signal.confidence)

        # Timing signal
        if bundle.timing_signal is not None:
            w = self._weights.get("timing", 0.0)
            score += bundle.timing_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.timing_signal.confidence)

        # Multi-market signal
        if bundle.multi_market_signal is not None:
            w = self._weights.get("multi_market", 0.0)
            score += bundle.multi_market_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.multi_market_signal.confidence)

        # Whale signal
        if bundle.whale_signal is not None:
            w = self._weights.get("whale", 0.0)
            score += bundle.whale_signal.confidence * w
            signals_triggered += 1
            max_single_confidence = max(max_single_confidence, bundle.whale_signal.confidence)

        # Multi-signal bonus
        if signals_triggered >= 4:
            score *= MULTI_SIGNAL_BONUS_4
        elif signals_triggered >= 3:
            score *= MULTI_SIGNAL_BONUS_3
        elif signals_triggered >= 2:
            score *= MULTI_SIGNAL_BONUS_2

        # Single high-confidence signal floor: ensure a very strong single
        # signal can still reach the info threshold on its own
        if max_single_confidence >= HIGH_CONFIDENCE_SINGLE_SIGNAL and score < self._info_threshold:
            score = self._info_threshold

        # Cap at 1.0
        score = min(score, 1.0)

        return score, signals_triggered

    async def _check_and_set_dedup(
        self,
        wallet_address: str,
        market_id: str,
    ) -> bool:
        key = f"{self._key_prefix}{wallet_address}:{market_id}"
        was_set = await self._redis.set(
            key,
            datetime.now(UTC).isoformat(),
            nx=True,
            ex=self._dedup_window,
        )
        return not was_set

    async def clear_dedup(
        self,
        wallet_address: str,
        market_id: str,
    ) -> bool:
        key = f"{self._key_prefix}{wallet_address}:{market_id}"
        deleted = await self._redis.delete(key)
        return int(deleted) > 0

    async def assess_batch(self, bundles: list[SignalBundle]) -> list[RiskAssessment]:
        import asyncio

        tasks = [self.assess(bundle) for bundle in bundles]
        return await asyncio.gather(*tasks)

    def get_weights(self) -> dict[str, float]:
        return self._weights.copy()

    def set_weights(self, weights: dict[str, float]) -> None:
        self._weights = weights.copy()
        logger.info("Updated risk scorer weights: %s", self._weights)
