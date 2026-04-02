"""Tests for the Polygon blockchain client."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web3.exceptions import Web3Exception

from polymarket_insider_tracker.profiler.chain import (
    BLOCK_CACHE_TTL,
    DEFAULT_CACHE_TTL_SECONDS,
    PolygonClient,
    RateLimiter,
    RPCError,
)
from polymarket_insider_tracker.profiler.rpc_provider import (
    RPCProvider,
    RPCProviderPool,
    _is_daily_limit_error,
)

# Valid Ethereum addresses for testing
VALID_ADDRESS = "0x742d35Cc6634C0532925a3b844Bc9e7595f5eaE2"
VALID_ADDRESS_2 = "0x8ba1f109551bD432803012645Ac136ddd64DBA72"
VALID_ADDRESS_3 = "0x1234567890AbCdEf1234567890ABcDeF12345678"
VALID_TOKEN = "0x7D1AfA7B718fb893dB30A3aBc0Cfc608AaCfeBB0"  # MATIC token


class TestRateLimiter:
    """Tests for the RateLimiter class."""

    def test_create(self) -> None:
        """Test creating a rate limiter."""
        limiter = RateLimiter.create(10.0)

        assert limiter.max_tokens == 10.0
        assert limiter.refill_rate == 10.0
        assert limiter.tokens == 10.0

    @pytest.mark.asyncio
    async def test_acquire_available(self) -> None:
        """Test acquiring when tokens are available."""
        limiter = RateLimiter.create(10.0)

        await limiter.acquire(1.0)

        assert limiter.tokens < 10.0

    @pytest.mark.asyncio
    async def test_acquire_multiple(self) -> None:
        """Test acquiring multiple tokens."""
        limiter = RateLimiter.create(10.0)

        for _ in range(5):
            await limiter.acquire(1.0)

        assert limiter.tokens < 6.0

    @pytest.mark.asyncio
    async def test_acquire_waits_when_empty(self) -> None:
        """Test that acquire waits when tokens are depleted."""
        limiter = RateLimiter.create(2.0)

        # Deplete tokens
        await limiter.acquire(2.0)

        # This should wait briefly for refill
        start = asyncio.get_event_loop().time()
        await limiter.acquire(0.5)
        elapsed = asyncio.get_event_loop().time() - start

        # Should have waited some time
        assert elapsed >= 0.1


class TestRPCProviderPool:
    """Tests for the RPCProviderPool class."""

    def test_init(self) -> None:
        """Test pool initialization."""
        pool = RPCProviderPool([("infura", "https://infura.io"), ("alchemy", "https://alchemy.com")])

        status = pool.get_status()
        assert len(status) == 2
        assert status[0]["name"] == "infura"
        assert status[1]["name"] == "alchemy"

    def test_init_empty_raises(self) -> None:
        """Test that empty provider list raises."""
        with pytest.raises(ValueError, match="At least one"):
            RPCProviderPool([])

    def test_get_ordered_providers_round_robin(self) -> None:
        """Test that providers rotate on successive calls."""
        pool = RPCProviderPool([("a", "https://a.com"), ("b", "https://b.com")])

        first = pool.get_ordered_providers()
        second = pool.get_ordered_providers()

        # First call starts at index 0, second at index 1
        assert first[0].name == "a"
        assert second[0].name == "b"

    def test_mark_unhealthy_skips_provider(self) -> None:
        """Test that unhealthy providers are placed last."""
        pool = RPCProviderPool(
            [("a", "https://a.com"), ("b", "https://b.com")],
            recovery_seconds=9999,  # Long recovery so it stays unhealthy
        )

        providers = pool.get_ordered_providers()
        pool.mark_unhealthy(providers[0])  # Mark "a" unhealthy

        ordered = pool.get_ordered_providers()
        # "b" should be first since "a" is unhealthy
        assert ordered[0].name == "b"

    def test_mark_daily_limited(self) -> None:
        """Test that daily-limited providers are excluded."""
        pool = RPCProviderPool([("a", "https://a.com"), ("b", "https://b.com")])

        providers = pool.get_ordered_providers()
        pool.mark_daily_limited(providers[0])

        status = pool.get_status()
        limited = [s for s in status if s["daily_limit_hit"]]
        assert len(limited) == 1
        assert limited[0]["name"] == "a"

    def test_mark_healthy_resets(self) -> None:
        """Test that marking healthy resets failure state."""
        pool = RPCProviderPool([("a", "https://a.com")])

        provider = pool.get_ordered_providers()[0]
        pool.mark_unhealthy(provider)
        assert not provider.healthy

        pool.mark_healthy(provider)
        assert provider.healthy
        assert provider.consecutive_failures == 0
        assert provider.requests_processed == 1

    def test_requests_processed_counter(self) -> None:
        """Test that requests_processed increments on mark_healthy."""
        pool = RPCProviderPool([("a", "https://a.com")])

        provider = pool.get_ordered_providers()[0]
        for _ in range(5):
            pool.mark_healthy(provider)

        assert provider.requests_processed == 5

    def test_get_status(self) -> None:
        """Test status output format."""
        pool = RPCProviderPool([("infura", "https://infura.io")])

        status = pool.get_status()
        assert len(status) == 1
        s = status[0]
        assert s["name"] == "infura"
        assert s["healthy"] is True
        assert s["daily_limit_hit"] is False
        assert s["consecutive_failures"] == 0
        assert s["requests_processed"] == 0


class TestDailyLimitDetection:
    """Tests for daily limit error detection."""

    def test_detects_infura_pattern(self) -> None:
        assert _is_daily_limit_error(Exception("daily request count exceeded"))

    def test_detects_alchemy_pattern(self) -> None:
        assert _is_daily_limit_error(Exception("Your app has exceeded its compute units"))

    def test_detects_generic_rate_limit(self) -> None:
        assert _is_daily_limit_error(Exception("Too Many Requests"))

    def test_ignores_regular_errors(self) -> None:
        assert not _is_daily_limit_error(Exception("Connection refused"))

    def test_case_insensitive(self) -> None:
        assert _is_daily_limit_error(Exception("DAILY REQUEST COUNT EXCEEDED"))


class TestPolygonClient:
    """Tests for the PolygonClient class."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    def test_init_with_rpc_url(self) -> None:
        """Test initialization with legacy rpc_url."""
        client = PolygonClient("https://polygon-rpc.com")
        assert client._cache_ttl == DEFAULT_CACHE_TTL_SECONDS

    def test_init_with_providers(self) -> None:
        """Test initialization with provider list."""
        client = PolygonClient(
            providers=[("infura", "https://infura.io"), ("alchemy", "https://alchemy.com")],
        )
        status = client.get_provider_status()
        assert len(status) == 2
        assert status[0]["name"] == "infura"

    def test_init_with_fallback(self) -> None:
        """Test initialization with legacy fallback."""
        client = PolygonClient(
            "https://polygon-rpc.com",
            fallback_rpc_url="https://fallback.com",
        )
        status = client.get_provider_status()
        assert len(status) == 2

    def test_init_no_url_raises(self) -> None:
        """Test that no URL raises."""
        with pytest.raises(ValueError):
            PolygonClient()

    def test_cache_key(self) -> None:
        """Test cache key generation."""
        client = PolygonClient("https://polygon-rpc.com")

        key = client._cache_key("nonce", "0xAbC123")

        assert key == "polygon:nonce:0xabc123"

    @pytest.mark.asyncio
    async def test_get_cached_miss(self, mock_redis: AsyncMock) -> None:
        """Test cache miss."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        result = await client._get_cached("test:key")

        assert result is None
        mock_redis.get.assert_called_once_with("test:key")

    @pytest.mark.asyncio
    async def test_get_cached_hit(self, mock_redis: AsyncMock) -> None:
        """Test cache hit."""
        mock_redis.get = AsyncMock(return_value=b"cached_value")
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        result = await client._get_cached("test:key")

        assert result == "cached_value"

    @pytest.mark.asyncio
    async def test_get_cached_error_handling(self, mock_redis: AsyncMock) -> None:
        """Test that cache errors are handled gracefully."""
        mock_redis.get = AsyncMock(side_effect=Exception("Redis error"))
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        result = await client._get_cached("test:key")

        assert result is None  # Should not raise

    @pytest.mark.asyncio
    async def test_set_cached(self, mock_redis: AsyncMock) -> None:
        """Test setting cache."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        await client._set_cached("test:key", "value")

        mock_redis.set.assert_called_once_with("test:key", "value", ex=DEFAULT_CACHE_TTL_SECONDS)

    @pytest.mark.asyncio
    async def test_set_cached_custom_ttl(self, mock_redis: AsyncMock) -> None:
        """Test setting cache with custom TTL."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        await client._set_cached("test:key", "value", ttl=3600)

        mock_redis.set.assert_called_once_with("test:key", "value", ex=3600)

    @pytest.mark.asyncio
    async def test_get_transaction_count_cached(self, mock_redis: AsyncMock) -> None:
        """Test getting transaction count from cache."""
        mock_redis.get = AsyncMock(return_value=b"42")
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        count = await client.get_transaction_count(VALID_ADDRESS)

        assert count == 42
        mock_redis.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_transaction_count_uncached(self, mock_redis: AsyncMock) -> None:
        """Test getting transaction count from blockchain."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "_execute_with_retry", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = 42

            count = await client.get_transaction_count(VALID_ADDRESS)

            assert count == 42
            mock_exec.assert_called_once()
            mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_transaction_counts_batch(self, mock_redis: AsyncMock) -> None:
        """Test batch getting transaction counts."""
        mock_redis.get = AsyncMock(side_effect=[b"10", None, b"30"])
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "get_transaction_count", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = 20

            addresses = [VALID_ADDRESS, VALID_ADDRESS_2, VALID_ADDRESS_3]
            counts = await client.get_transaction_counts(addresses)

            assert counts[VALID_ADDRESS.lower()] == 10  # From cache
            assert counts[VALID_ADDRESS_2.lower()] == 20  # From blockchain
            assert counts[VALID_ADDRESS_3.lower()] == 30  # From cache

    @pytest.mark.asyncio
    async def test_get_transaction_counts_empty(self, mock_redis: AsyncMock) -> None:
        """Test batch with empty list."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        counts = await client.get_transaction_counts([])

        assert counts == {}

    @pytest.mark.asyncio
    async def test_get_balance_cached(self, mock_redis: AsyncMock) -> None:
        """Test getting balance from cache."""
        mock_redis.get = AsyncMock(return_value=b"1000000000000000000")
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        balance = await client.get_balance(VALID_ADDRESS)

        assert balance == Decimal("1000000000000000000")

    @pytest.mark.asyncio
    async def test_get_balance_uncached(self, mock_redis: AsyncMock) -> None:
        """Test getting balance from blockchain."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "_execute_with_retry", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = 2000000000000000000

            balance = await client.get_balance(VALID_ADDRESS)

            assert balance == Decimal("2000000000000000000")
            mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_wallet_info(self, mock_redis: AsyncMock) -> None:
        """Test getting aggregated wallet info."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with (
            patch.object(client, "get_transaction_count", new_callable=AsyncMock) as mock_nonce,
            patch.object(client, "get_balance", new_callable=AsyncMock) as mock_balance,
            patch.object(client, "get_first_transaction", new_callable=AsyncMock) as mock_tx,
        ):
            mock_nonce.return_value = 42
            mock_balance.return_value = Decimal("1000000000000000000")
            mock_tx.return_value = None

            info = await client.get_wallet_info(VALID_ADDRESS)

            assert info.address == VALID_ADDRESS.lower()
            assert info.transaction_count == 42
            assert info.balance_wei == Decimal("1000000000000000000")
            assert info.first_transaction is None

    @pytest.mark.asyncio
    async def test_get_first_transaction_no_transactions(self, mock_redis: AsyncMock) -> None:
        """Test get_first_transaction when wallet has no transactions."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "get_transaction_count", new_callable=AsyncMock) as mock_nonce:
            mock_nonce.return_value = 0

            tx = await client.get_first_transaction(VALID_ADDRESS)

            assert tx is None

    @pytest.mark.asyncio
    async def test_health_check_success(self, mock_redis: AsyncMock) -> None:
        """Test successful health check."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "_execute_with_retry", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = 50000000

            healthy = await client.health_check()

            assert healthy is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, mock_redis: AsyncMock) -> None:
        """Test failed health check."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "_execute_with_retry", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = RPCError("Connection failed")

            healthy = await client.health_check()

            assert healthy is False

    @pytest.mark.asyncio
    async def test_get_provider_status(self) -> None:
        """Test get_provider_status returns correct format."""
        client = PolygonClient(
            providers=[("infura", "https://infura.io"), ("alchemy", "https://alchemy.com")],
        )

        status = client.get_provider_status()

        assert len(status) == 2
        assert status[0]["name"] == "infura"
        assert status[0]["healthy"] is True
        assert status[0]["requests_processed"] == 0


class TestPolygonClientRetryLogic:
    """Tests for retry and failover logic."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, mock_redis: AsyncMock) -> None:
        """Test that client retries on RPC failure."""
        client = PolygonClient(
            "https://polygon-rpc.com",
            redis=mock_redis,
            max_retries=3,
            retry_delay_seconds=0.01,
        )

        call_count = 0

        async def mock_get_tx_count(*_args: object, **_kwargs: object) -> int:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Web3Exception("Temporary error")
            return 42

        # Patch the provider's w3 instance
        provider = client._provider_pool._providers[0]
        provider.w3.eth.get_transaction_count = mock_get_tx_count

        count = await client.get_transaction_count(VALID_ADDRESS)

        assert count == 42
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_failover_to_next_provider(self, mock_redis: AsyncMock) -> None:
        """Test failover to next provider in pool."""
        client = PolygonClient(
            providers=[("primary", "https://primary.com"), ("backup", "https://backup.com")],
            redis=mock_redis,
            max_retries=1,
            retry_delay_seconds=0.01,
        )

        primary = client._provider_pool._providers[0]
        backup = client._provider_pool._providers[1]

        # Primary always fails
        async def primary_fail(*_args: object, **_kwargs: object) -> int:
            raise Web3Exception("Primary down")

        primary.w3.eth.get_transaction_count = primary_fail
        backup.w3.eth.get_transaction_count = AsyncMock(return_value=42)

        count = await client.get_transaction_count(VALID_ADDRESS)

        assert count == 42
        assert not primary.healthy

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, mock_redis: AsyncMock) -> None:
        """Test error when all retries are exhausted."""
        client = PolygonClient(
            "https://polygon-rpc.com",
            redis=mock_redis,
            max_retries=2,
            retry_delay_seconds=0.01,
        )

        provider = client._provider_pool._providers[0]

        async def always_fail(*_args: object, **_kwargs: object) -> int:
            raise Web3Exception("Always fails")

        provider.w3.eth.get_transaction_count = always_fail

        with pytest.raises(RPCError):
            await client.get_transaction_count(VALID_ADDRESS)

    @pytest.mark.asyncio
    async def test_daily_limit_moves_to_next_provider(self, mock_redis: AsyncMock) -> None:
        """Test that daily limit error triggers provider switch."""
        client = PolygonClient(
            providers=[("infura", "https://infura.io"), ("alchemy", "https://alchemy.com")],
            redis=mock_redis,
            max_retries=1,
            retry_delay_seconds=0.01,
        )

        infura = client._provider_pool._providers[0]
        alchemy = client._provider_pool._providers[1]

        async def daily_limit_fail(*_args: object, **_kwargs: object) -> int:
            raise Web3Exception("daily request count exceeded")

        infura.w3.eth.get_transaction_count = daily_limit_fail
        alchemy.w3.eth.get_transaction_count = AsyncMock(return_value=42)

        count = await client.get_transaction_count(VALID_ADDRESS)

        assert count == 42
        assert infura.daily_limit_hit


class TestPolygonClientTokenBalance:
    """Tests for ERC20 token balance queries."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_get_token_balance_cached(self, mock_redis: AsyncMock) -> None:
        """Test getting token balance from cache."""
        mock_redis.get = AsyncMock(return_value=b"1000000")
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        balance = await client.get_token_balance(VALID_ADDRESS, VALID_TOKEN)

        assert balance == Decimal("1000000")

    @pytest.mark.asyncio
    async def test_get_token_balance_uncached(self, mock_redis: AsyncMock) -> None:
        """Test getting token balance from blockchain."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(
            client, "_execute_contract_call", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = 5000000

            balance = await client.get_token_balance(VALID_ADDRESS, VALID_TOKEN)

            assert balance == Decimal("5000000")
            mock_redis.set.assert_called_once()


class TestPolygonClientBlock:
    """Tests for block queries."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_get_block_cached(self, mock_redis: AsyncMock) -> None:
        """Test getting block from cache."""
        mock_redis.get = AsyncMock(return_value=b'{"timestamp": 1704369600}')
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        block = await client.get_block(50000000)

        assert block["timestamp"] == 1704369600

    @pytest.mark.asyncio
    async def test_get_block_uncached(self, mock_redis: AsyncMock) -> None:
        """Test getting block from blockchain."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        with patch.object(client, "_execute_with_retry", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"timestamp": 1704369600, "number": 50000000}

            block = await client.get_block(50000000)

            assert block["timestamp"] == 1704369600
            # Block cache uses 24h TTL
            mock_redis.set.assert_called_once()
            call_args = mock_redis.set.call_args
            assert call_args[1]["ex"] == BLOCK_CACHE_TTL


class TestPolygonClientGetLogs:
    """Tests for the get_logs method."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis client."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_get_logs_cached(self, mock_redis: AsyncMock) -> None:
        """Test getting logs from cache."""
        mock_redis.get = AsyncMock(return_value=b'[{"blockNumber": 100}]')
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        logs = await client.get_logs({"address": "0x123", "fromBlock": 0, "toBlock": 100})

        assert len(logs) == 1
        assert logs[0]["blockNumber"] == 100

    @pytest.mark.asyncio
    async def test_get_logs_uncached(self, mock_redis: AsyncMock) -> None:
        """Test getting logs from blockchain."""
        client = PolygonClient("https://polygon-rpc.com", redis=mock_redis)

        provider = client._provider_pool._providers[0]
        mock_log = MagicMock()
        mock_log.__iter__ = MagicMock(return_value=iter([("blockNumber", 100)]))
        mock_log.keys = MagicMock(return_value=["blockNumber"])
        mock_log.__getitem__ = MagicMock(side_effect=lambda k: 100 if k == "blockNumber" else None)
        provider.w3.eth.get_logs = AsyncMock(return_value=[])

        logs = await client.get_logs({"address": "0x123", "fromBlock": 0, "toBlock": 100})

        assert logs == []
        provider.w3.eth.get_logs.assert_called_once()
