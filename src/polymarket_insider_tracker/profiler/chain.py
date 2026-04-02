"""Polygon blockchain client with connection pooling and caching.

This module provides a Polygon client for wallet data queries with:
- Multi-provider rotation for load distribution
- Redis caching to avoid redundant RPC calls
- Retry logic with exponential backoff
- Rate limiting to respect provider limits
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from redis.asyncio import Redis
from web3 import AsyncWeb3
from web3.exceptions import Web3Exception

from polymarket_insider_tracker.profiler.models import Transaction, WalletInfo

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_CACHE_TTL_SECONDS = 300  # 5 minutes
DEFAULT_MAX_REQUESTS_PER_SECOND = 25
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 1.0
DEFAULT_CONNECTION_POOL_SIZE = 10
DEFAULT_REQUEST_TIMEOUT = 30

# Extended TTLs for immutable data
BLOCK_CACHE_TTL = 86400  # 24 hours — block data is immutable
FIRST_TX_CACHE_TTL = 86400  # 24 hours — first tx never changes
FIRST_TX_NULL_CACHE_TTL = 60  # 1 minute — wallet might get a tx soon
LOGS_CACHE_TTL = 3600  # 1 hour — historical logs are immutable
LOGS_LATEST_CACHE_TTL = 300  # 5 minutes — latest block range may change


class PolygonClientError(Exception):
    """Base exception for Polygon client errors."""


class RPCError(PolygonClientError):
    """Raised when RPC call fails."""


class RateLimitError(PolygonClientError):
    """Raised when rate limit is exceeded."""


@dataclass
class RateLimiter:
    """Token bucket rate limiter."""

    max_tokens: float
    refill_rate: float  # tokens per second
    tokens: float
    last_refill: float

    @classmethod
    def create(cls, max_requests_per_second: float) -> "RateLimiter":
        """Create a rate limiter with specified max requests per second."""
        return cls(
            max_tokens=max_requests_per_second,
            refill_rate=max_requests_per_second,
            tokens=max_requests_per_second,
            last_refill=time.monotonic(),
        )

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Acquire tokens, waiting if necessary."""
        while True:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
            # Wait for tokens to refill
            wait_time = (tokens - self.tokens) / self.refill_rate
            await asyncio.sleep(wait_time)


class PolygonClient:
    """Polygon blockchain client with caching and rate limiting.

    Provides efficient access to wallet data with:
    - Multi-provider rotation for load distribution
    - Redis caching with configurable TTL
    - Rate limiting to respect provider limits
    - Retry logic with exponential backoff

    Example:
        ```python
        redis = Redis.from_url("redis://localhost:6379")
        client = PolygonClient(
            providers=[("infura", "https://..."), ("alchemy", "https://...")],
            redis=redis,
        )

        # Get single wallet info
        nonce = await client.get_transaction_count("0x...")

        # Batch query multiple wallets
        nonces = await client.get_transaction_counts(["0x...", "0x..."])
        ```
    """

    def __init__(
        self,
        rpc_url: str = "",
        *,
        providers: list[tuple[str, str]] | None = None,
        fallback_rpc_url: str | None = None,
        redis: Redis | None = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        max_requests_per_second: float = DEFAULT_MAX_REQUESTS_PER_SECOND,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        """Initialize the Polygon client.

        Args:
            rpc_url: Primary RPC URL (backward compat). Ignored if providers is set.
            providers: List of (name, url) tuples for provider rotation.
            fallback_rpc_url: Legacy fallback URL (backward compat).
            redis: Optional Redis client for caching.
            cache_ttl_seconds: Default cache TTL in seconds.
            max_requests_per_second: Rate limit per provider.
            max_retries: Maximum retry attempts per provider.
            retry_delay_seconds: Initial delay between retries.
        """
        from polymarket_insider_tracker.profiler.rpc_provider import RPCProviderPool

        # Normalize to provider list
        if providers:
            provider_list = providers
        elif rpc_url:
            provider_list = [("default", rpc_url)]
            if fallback_rpc_url:
                provider_list.append(("fallback", fallback_rpc_url))
        else:
            raise ValueError("Either rpc_url or providers must be specified")

        self._provider_pool = RPCProviderPool(
            provider_list,
            max_requests_per_second=max_requests_per_second,
        )
        self._redis = redis
        self._cache_ttl = cache_ttl_seconds
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds

        # Cache key prefix
        self._cache_prefix = "polygon:"

    def _cache_key(self, key_type: str, address: str) -> str:
        """Generate a cache key."""
        return f"{self._cache_prefix}{key_type}:{address.lower()}"

    async def _get_cached(self, key: str) -> str | None:
        """Get value from cache."""
        if not self._redis:
            return None
        try:
            value = await self._redis.get(key)
            if isinstance(value, bytes):
                return value.decode()
            return str(value) if value is not None else None
        except Exception as e:
            logger.warning("Cache get failed: %s", e)
            return None

    async def _set_cached(self, key: str, value: str, ttl: int | None = None) -> None:
        """Set value in cache."""
        if not self._redis:
            return
        try:
            await self._redis.set(key, value, ex=ttl or self._cache_ttl)
        except Exception as e:
            logger.warning("Cache set failed: %s", e)

    async def _execute_with_retry(
        self,
        func_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute an RPC call with retry and provider rotation.

        Tries each available provider with exponential backoff retries.
        Marks providers as unhealthy or daily-limited as appropriate.

        Args:
            func_name: Name of the web3.eth method to call.
            *args: Positional arguments for the method.
            **kwargs: Keyword arguments for the method.

        Returns:
            Result from the RPC call.

        Raises:
            RPCError: If all providers and retries are exhausted.
        """
        from polymarket_insider_tracker.profiler.rpc_provider import _is_daily_limit_error

        providers = self._provider_pool.get_ordered_providers()
        if not providers:
            raise RPCError("All RPC providers are unavailable")

        last_error: Exception | None = None

        for provider in providers:
            delay = self._retry_delay
            for attempt in range(self._max_retries):
                await provider.rate_limiter.acquire()
                try:
                    method = getattr(provider.w3.eth, func_name)
                    result = await method(*args, **kwargs)
                    self._provider_pool.mark_healthy(provider)
                    return result
                except Web3Exception as e:
                    last_error = e
                    if _is_daily_limit_error(e):
                        self._provider_pool.mark_daily_limited(provider)
                        break  # Move to next provider
                    logger.warning(
                        "Provider %s %s failed (attempt %d/%d): %s",
                        provider.name,
                        func_name,
                        attempt + 1,
                        self._max_retries,
                        e,
                    )
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(delay)
                        delay *= 2
            else:
                # All retries exhausted for this provider
                self._provider_pool.mark_unhealthy(provider)

        raise RPCError(f"RPC call {func_name} failed on all providers: {last_error}")

    async def _execute_contract_call(
        self,
        contract_address: str,
        abi: list[dict],
        method_name: str,
        *args: Any,
    ) -> Any:
        """Execute a contract call with provider rotation.

        Args:
            contract_address: Contract address.
            abi: Contract ABI.
            method_name: Contract method name.
            *args: Method arguments.

        Returns:
            Result from the contract call.

        Raises:
            RPCError: If all providers fail.
        """
        from polymarket_insider_tracker.profiler.rpc_provider import _is_daily_limit_error

        providers = self._provider_pool.get_ordered_providers()
        if not providers:
            raise RPCError("All RPC providers are unavailable")

        last_error: Exception | None = None

        for provider in providers:
            await provider.rate_limiter.acquire()
            try:
                contract = provider.w3.eth.contract(
                    address=AsyncWeb3.to_checksum_address(contract_address),
                    abi=abi,
                )
                result = await getattr(contract.functions, method_name)(*args).call()
                self._provider_pool.mark_healthy(provider)
                return result
            except Web3Exception as e:
                last_error = e
                if _is_daily_limit_error(e):
                    self._provider_pool.mark_daily_limited(provider)
                else:
                    self._provider_pool.mark_unhealthy(provider)

        raise RPCError(f"Contract call {method_name} failed on all providers: {last_error}")

    async def get_transaction_count(self, address: str) -> int:
        """Get wallet transaction count (nonce).

        Args:
            address: Wallet address.

        Returns:
            Transaction count.
        """
        cache_key = self._cache_key("nonce", address)

        # Check cache
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return int(cached)

        # Query blockchain
        count = await self._execute_with_retry(
            "get_transaction_count",
            AsyncWeb3.to_checksum_address(address),
        )

        # Cache result
        await self._set_cached(cache_key, str(count))

        return int(count)

    async def get_transaction_counts(
        self,
        addresses: Sequence[str],
    ) -> dict[str, int]:
        """Batch get transaction counts for multiple addresses.

        Args:
            addresses: List of wallet addresses.

        Returns:
            Dictionary mapping address to transaction count.
        """
        if not addresses:
            return {}

        results: dict[str, int] = {}
        uncached: list[str] = []

        # Check cache for each address
        for address in addresses:
            cache_key = self._cache_key("nonce", address)
            cached = await self._get_cached(cache_key)
            if cached is not None:
                results[address.lower()] = int(cached)
            else:
                uncached.append(address)

        # Query uncached addresses concurrently
        if uncached:
            tasks = [self.get_transaction_count(addr) for addr in uncached]
            counts = await asyncio.gather(*tasks, return_exceptions=True)

            for addr, count in zip(uncached, counts, strict=True):
                if isinstance(count, BaseException):
                    logger.warning("Failed to get nonce for %s: %s", addr, count)
                    results[addr.lower()] = 0
                else:
                    results[addr.lower()] = count

        return results

    async def get_balance(self, address: str) -> Decimal:
        """Get wallet MATIC balance in Wei.

        Args:
            address: Wallet address.

        Returns:
            Balance in Wei as Decimal.
        """
        cache_key = self._cache_key("balance", address)

        # Check cache
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return Decimal(cached)

        # Query blockchain
        balance = await self._execute_with_retry(
            "get_balance",
            AsyncWeb3.to_checksum_address(address),
        )

        # Cache result
        await self._set_cached(cache_key, str(balance))

        return Decimal(balance)

    async def get_token_balance(
        self,
        address: str,
        token_address: str,
    ) -> Decimal:
        """Get ERC20 token balance.

        Args:
            address: Wallet address.
            token_address: ERC20 token contract address.

        Returns:
            Token balance in smallest unit as Decimal.
        """
        cache_key = self._cache_key(f"token:{token_address.lower()}", address)

        # Check cache
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return Decimal(cached)

        # ERC20 balanceOf ABI
        erc20_abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function",
            }
        ]

        balance = await self._execute_contract_call(
            token_address,
            erc20_abi,
            "balanceOf",
            AsyncWeb3.to_checksum_address(address),
        )

        # Cache result
        await self._set_cached(cache_key, str(balance))

        return Decimal(balance)

    async def get_block(self, block_number: int) -> dict[str, Any]:
        """Get block by number.

        Args:
            block_number: Block number.

        Returns:
            Block data dictionary.
        """
        cache_key = f"{self._cache_prefix}block:{block_number}"

        # Check cache
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cast(dict[str, Any], json.loads(cached))

        block = await self._execute_with_retry("get_block", block_number)

        # Convert to serializable dict
        block_dict = dict(block)
        block_dict["timestamp"] = int(block_dict["timestamp"])

        # Cache result (blocks are immutable, use longer TTL)
        await self._set_cached(cache_key, json.dumps(block_dict), ttl=BLOCK_CACHE_TTL)

        return dict(block_dict)

    async def get_first_transaction(self, address: str) -> Transaction | None:
        """Get the first transaction for a wallet.

        This is useful for determining wallet age. Note: This is an expensive
        operation as it may require scanning transaction history.

        Args:
            address: Wallet address.

        Returns:
            First transaction or None if no transactions.
        """
        cache_key = self._cache_key("first_tx", address)

        # Check cache
        cached = await self._get_cached(cache_key)
        if cached is not None:
            if cached == "null":
                return None
            data = json.loads(cached)
            return Transaction(
                hash=data["hash"],
                block_number=data["block_number"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                from_address=data["from_address"],
                to_address=data["to_address"],
                value=Decimal(data["value"]),
                gas_used=data["gas_used"],
                gas_price=Decimal(data["gas_price"]),
            )

        # Check if wallet has any transactions
        nonce = await self.get_transaction_count(address)
        if nonce == 0:
            await self._set_cached(cache_key, "null", ttl=FIRST_TX_NULL_CACHE_TTL)
            return None

        # Note: Getting the actual first transaction requires using an indexer
        # or scanning blocks, which is expensive. For now, we'll return None
        # and recommend using an indexer service for production.
        logger.warning(
            "get_first_transaction requires an indexer service for %s (nonce=%d)",
            address,
            nonce,
        )
        return None

    async def get_wallet_info(self, address: str) -> WalletInfo:
        """Get aggregated wallet information.

        Args:
            address: Wallet address.

        Returns:
            WalletInfo with transaction count, balance, and first transaction.
        """
        # Fetch data concurrently
        nonce_task = self.get_transaction_count(address)
        balance_task = self.get_balance(address)
        first_tx_task = self.get_first_transaction(address)

        nonce, balance, first_tx = await asyncio.gather(nonce_task, balance_task, first_tx_task)

        return WalletInfo(
            address=address.lower(),
            transaction_count=nonce,
            balance_wei=balance,
            first_transaction=first_tx,
        )

    async def get_logs(self, filter_params: dict[str, Any]) -> list[dict[str, Any]]:
        """Get event logs with provider rotation and caching.

        Args:
            filter_params: Web3 filter parameters (address, topics, fromBlock, toBlock).

        Returns:
            List of log dictionaries.

        Raises:
            RPCError: If all providers fail.
        """
        from polymarket_insider_tracker.profiler.rpc_provider import _is_daily_limit_error

        # Generate cache key from filter params
        cache_key = self._logs_cache_key(filter_params)

        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cast(list[dict[str, Any]], json.loads(cached))

        # Determine TTL based on whether toBlock is "latest"
        to_block = filter_params.get("toBlock", "latest")
        ttl = LOGS_LATEST_CACHE_TTL if to_block == "latest" else LOGS_CACHE_TTL

        # Execute with provider rotation
        providers = self._provider_pool.get_ordered_providers()
        if not providers:
            raise RPCError("All RPC providers are unavailable")

        last_error: Exception | None = None
        for provider in providers:
            await provider.rate_limiter.acquire()
            try:
                logs = await provider.w3.eth.get_logs(filter_params)
                result = [_serialize_log(log) for log in logs]
                await self._set_cached(cache_key, json.dumps(result), ttl=ttl)
                self._provider_pool.mark_healthy(provider)
                return result
            except Web3Exception as e:
                last_error = e
                if _is_daily_limit_error(e):
                    self._provider_pool.mark_daily_limited(provider)
                else:
                    self._provider_pool.mark_unhealthy(provider)

        raise RPCError(f"get_logs failed on all providers: {last_error}")

    def _logs_cache_key(self, filter_params: dict[str, Any]) -> str:
        """Generate a stable cache key for log filter params."""
        # Serialize deterministically for hashing
        serializable = json.dumps(filter_params, sort_keys=True, default=str)
        digest = hashlib.sha256(serializable.encode()).hexdigest()[:16]
        return f"{self._cache_prefix}logs:{digest}"

    def get_provider_status(self) -> list[dict]:
        """Return status of all RPC providers for the health endpoint."""
        return self._provider_pool.get_status()

    async def health_check(self) -> bool:
        """Check if the client can connect to the RPC.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            await self._execute_with_retry("block_number")
            return True
        except RPCError:
            return False


def _serialize_log(log: Any) -> dict[str, Any]:
    """Convert a web3 log object to a JSON-serializable dict."""
    d = dict(log)
    # Convert HexBytes to hex strings
    for key in ("transactionHash", "blockHash"):
        if key in d and hasattr(d[key], "hex"):
            d[key] = d[key].hex()
    if "topics" in d:
        d["topics"] = [t.hex() if hasattr(t, "hex") else str(t) for t in d["topics"]]
    if "data" in d and hasattr(d["data"], "hex"):
        d["data"] = d["data"].hex()
    return d
