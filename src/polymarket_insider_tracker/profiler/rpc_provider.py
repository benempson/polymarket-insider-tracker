"""RPC provider pool with round-robin rotation and health tracking.

Manages multiple Polygon RPC providers, distributing requests evenly
and handling transient failures and daily rate limit exhaustion.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from polymarket_insider_tracker.profiler.chain import RateLimiter

logger = logging.getLogger(__name__)

# Known error patterns indicating daily rate limit exhaustion
_DAILY_LIMIT_PATTERNS = [
    "daily request count exceeded",
    "exceeded its compute units",
    "too many requests",
    "rate limit reached",
    "request rate exceeded",
]

DEFAULT_RECOVERY_SECONDS = 60.0
DEFAULT_DAILY_LIMIT_RECOVERY_SECONDS = 3600.0


def _is_daily_limit_error(error: Exception) -> bool:
    """Check if an exception indicates daily rate limit exhaustion."""
    msg = str(error).lower()
    return any(pattern in msg for pattern in _DAILY_LIMIT_PATTERNS)


@dataclass
class RPCProvider:
    """A single RPC provider endpoint with health state."""

    name: str
    url: str
    w3: AsyncWeb3
    rate_limiter: RateLimiter
    healthy: bool = True
    daily_limit_hit: bool = False
    daily_limit_reset_at: float = 0.0
    last_failure_at: float = 0.0
    consecutive_failures: int = 0
    requests_processed: int = 0


class RPCProviderPool:
    """Pool of RPC providers with round-robin rotation and health tracking.

    Distributes requests across providers evenly, skipping unhealthy
    or rate-limited ones. Providers recover automatically after a
    configurable interval.
    """

    def __init__(
        self,
        providers: list[tuple[str, str]],
        *,
        max_requests_per_second: float = 25.0,
        recovery_seconds: float = DEFAULT_RECOVERY_SECONDS,
        daily_limit_recovery_seconds: float = DEFAULT_DAILY_LIMIT_RECOVERY_SECONDS,
    ) -> None:
        """Initialize the provider pool.

        Args:
            providers: List of (name, url) tuples for each provider.
            max_requests_per_second: Rate limit per provider.
            recovery_seconds: Seconds before retrying an unhealthy provider.
            daily_limit_recovery_seconds: Seconds before retrying a rate-limited provider.
        """
        if not providers:
            raise ValueError("At least one RPC provider is required")

        self._providers: list[RPCProvider] = []
        for name, url in providers:
            self._providers.append(
                RPCProvider(
                    name=name,
                    url=url,
                    w3=AsyncWeb3(AsyncHTTPProvider(url)),
                    rate_limiter=RateLimiter.create(max_requests_per_second),
                )
            )

        self._rotation_index = 0
        self._recovery_seconds = recovery_seconds
        self._daily_limit_recovery_seconds = daily_limit_recovery_seconds

        logger.info(
            "RPC provider pool initialized with %d providers: %s",
            len(self._providers),
            ", ".join(p.name for p in self._providers),
        )

    def _is_available(self, provider: RPCProvider) -> bool:
        """Check if a provider is available for requests."""
        now = time.monotonic()

        if provider.daily_limit_hit:
            if now >= provider.daily_limit_reset_at:
                provider.daily_limit_hit = False
                logger.info("Provider %s daily limit reset, re-enabling", provider.name)
            else:
                return False

        if not provider.healthy:
            if now - provider.last_failure_at >= self._recovery_seconds:
                return True  # Allow recovery attempt
            return False

        return True

    def get_ordered_providers(self) -> list[RPCProvider]:
        """Return available providers starting from the next in rotation.

        Advances the rotation index so subsequent calls distribute load.
        Unhealthy and rate-limited providers are placed at the end.

        Returns:
            Ordered list of providers (available first, then recovery candidates).
        """
        n = len(self._providers)
        available = []
        recovery = []

        for i in range(n):
            idx = (self._rotation_index + i) % n
            provider = self._providers[idx]
            if self._is_available(provider):
                if provider.healthy:
                    available.append(provider)
                else:
                    recovery.append(provider)

        # Advance rotation for next call
        self._rotation_index = (self._rotation_index + 1) % n

        return available + recovery

    def mark_healthy(self, provider: RPCProvider) -> None:
        """Mark a provider as healthy after a successful request."""
        if not provider.healthy:
            logger.info("Provider %s recovered", provider.name)
        provider.healthy = True
        provider.consecutive_failures = 0
        provider.requests_processed += 1

    def mark_unhealthy(self, provider: RPCProvider) -> None:
        """Mark a provider as unhealthy after failures."""
        provider.healthy = False
        provider.consecutive_failures += 1
        provider.last_failure_at = time.monotonic()
        logger.warning(
            "Provider %s marked unhealthy (failures: %d)",
            provider.name,
            provider.consecutive_failures,
        )

    def mark_daily_limited(self, provider: RPCProvider) -> None:
        """Mark a provider as having hit its daily rate limit."""
        provider.daily_limit_hit = True
        provider.daily_limit_reset_at = (
            time.monotonic() + self._daily_limit_recovery_seconds
        )
        logger.warning(
            "Provider %s hit daily rate limit, disabled for %ds",
            provider.name,
            int(self._daily_limit_recovery_seconds),
        )

    def get_status(self) -> list[dict]:
        """Return status of all providers for the health endpoint."""
        return [
            {
                "name": p.name,
                "healthy": p.healthy,
                "daily_limit_hit": p.daily_limit_hit,
                "consecutive_failures": p.consecutive_failures,
                "requests_processed": p.requests_processed,
            }
            for p in self._providers
        ]
