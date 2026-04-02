"""Conviction / edge analysis — detect trades at extreme prices."""

from __future__ import annotations

import logging
from decimal import Decimal

from polymarket_insider_tracker.detector.models import ConvictionSignal
from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)

DEFAULT_MIN_TRADE_SIZE = Decimal("2000")
DEFAULT_EXTREME_LOW = Decimal("0.20")
DEFAULT_EXTREME_HIGH = Decimal("0.80")


class ConvictionDetector:
    """Detect trades that imply strong directional conviction.

    Buying a low-priced outcome (< 0.20) with size is a contrarian bet
    requiring high conviction.  Buying a high-priced outcome (> 0.80)
    is confirming consensus but has limited upside — lower signal.
    """

    def __init__(
        self,
        *,
        min_trade_size: Decimal = DEFAULT_MIN_TRADE_SIZE,
        extreme_low: Decimal = DEFAULT_EXTREME_LOW,
        extreme_high: Decimal = DEFAULT_EXTREME_HIGH,
    ) -> None:
        self._min_trade_size = min_trade_size
        self._extreme_low = extreme_low
        self._extreme_high = extreme_high

    async def analyze(self, trade: TradeEvent) -> ConvictionSignal | None:
        if trade.notional_value < self._min_trade_size:
            return None

        price = trade.price
        # Price extremity: distance from 0.50 (ranges 0.0 – 0.5)
        price_extremity = abs(price - Decimal("0.50"))

        if price_extremity < Decimal("0.30"):
            # Price is between 0.20 and 0.80 — not extreme enough
            return None

        # Is this a contrarian bet?
        # Buying an outcome priced below 0.20 means betting against consensus.
        is_contrarian = trade.is_buy and price < self._extreme_low

        # Confidence calculation
        # Base: how extreme the price is (normalised 0-1 from the 0.30-0.50 range)
        extremity_score = float((price_extremity - Decimal("0.30")) / Decimal("0.20"))
        extremity_score = min(extremity_score, 1.0)

        confidence = extremity_score * 0.5  # up to 0.5 from price

        # Contrarian bonus
        if is_contrarian:
            confidence += 0.3

        # Size bonus: larger trades show more conviction
        if trade.notional_value >= Decimal("10000"):
            confidence += 0.2
        elif trade.notional_value >= Decimal("5000"):
            confidence += 0.1

        confidence = max(0.0, min(1.0, confidence))

        if confidence < 0.2:
            return None

        factors = {
            "price_extremity": extremity_score,
            "contrarian": 0.3 if is_contrarian else 0.0,
            "size_bonus": confidence - extremity_score * 0.5 - (0.3 if is_contrarian else 0.0),
        }

        logger.info(
            "Conviction signal: market=%s, price=%.3f, contrarian=%s, confidence=%.2f",
            trade.market_id[:10] + "...",
            price,
            is_contrarian,
            confidence,
        )

        return ConvictionSignal(
            trade_event=trade,
            price_extremity=float(price_extremity),
            is_contrarian=is_contrarian,
            confidence=confidence,
            factors=factors,
        )
