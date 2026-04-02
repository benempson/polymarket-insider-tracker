"""Timing-based detection — trades near market expiry."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from polymarket_insider_tracker.detector.models import TimingSignal
from polymarket_insider_tracker.ingestor.metadata_sync import MarketMetadataSync
from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)

DEFAULT_CRITICAL_HOURS = 24.0
DEFAULT_MIN_TRADE_SIZE = Decimal("2000")
DEFAULT_MIN_DIRECTIONALITY = Decimal("0.30")  # price distance from 0.50


class TimingDetector:
    """Detect large directional trades placed close to market expiry.

    A trade in the final hours before resolution with a strong
    directional bet implies the trader has near-term information.
    """

    def __init__(
        self,
        metadata_sync: MarketMetadataSync,
        *,
        critical_hours: float = DEFAULT_CRITICAL_HOURS,
        min_trade_size: Decimal = DEFAULT_MIN_TRADE_SIZE,
        min_directionality: Decimal = DEFAULT_MIN_DIRECTIONALITY,
    ) -> None:
        self._metadata_sync = metadata_sync
        self._critical_hours = critical_hours
        self._min_trade_size = min_trade_size
        self._min_directionality = min_directionality

    async def analyze(self, trade: TradeEvent) -> TimingSignal | None:
        if trade.notional_value < self._min_trade_size:
            return None

        try:
            metadata = await self._metadata_sync.get_market(trade.market_id)
        except Exception:
            return None

        if metadata is None or metadata.end_date is None:
            return None

        end_dt = metadata.end_date
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        hours_remaining = (end_dt - now).total_seconds() / 3600

        if hours_remaining <= 0 or hours_remaining > self._critical_hours:
            return None

        # Directionality: how far from 0.50 is the price?
        directionality = abs(trade.price - Decimal("0.50"))
        if directionality < self._min_directionality:
            return None

        # Confidence: closer to expiry + stronger direction = higher
        time_score = 1.0 - (hours_remaining / self._critical_hours)  # 0..1
        dir_score = float(directionality / Decimal("0.50"))  # 0..1

        confidence = time_score * 0.5 + dir_score * 0.3

        # Size bonus
        if trade.notional_value >= Decimal("10000"):
            confidence += 0.2
        elif trade.notional_value >= Decimal("5000"):
            confidence += 0.1

        confidence = max(0.0, min(1.0, confidence))

        if confidence < 0.2:
            return None

        factors = {
            "hours_remaining": hours_remaining,
            "time_score": time_score,
            "directionality": float(directionality),
        }

        logger.info(
            "Timing signal: market=%s, hours_remaining=%.1f, dir=%.2f, confidence=%.2f",
            trade.market_id[:10] + "...",
            hours_remaining,
            float(directionality),
            confidence,
        )

        return TimingSignal(
            trade_event=trade,
            hours_to_expiry=hours_remaining,
            market_end_date=end_dt,
            confidence=confidence,
            factors=factors,
        )
