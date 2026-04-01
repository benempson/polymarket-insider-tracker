"""Main pipeline orchestrator for Polymarket Insider Tracker.

This module provides the Pipeline class that wires together all detection
components and manages the event flow from ingestion to alerting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from polymarket_insider_tracker.alerter.channels.discord import DiscordChannel
from polymarket_insider_tracker.alerter.channels.telegram import TelegramChannel
from polymarket_insider_tracker.alerter.dispatcher import AlertChannel, AlertDispatcher
from polymarket_insider_tracker.alerter.formatter import AlertFormatter
from polymarket_insider_tracker.alerter.models import FormattedAlert
from polymarket_insider_tracker.alerter.notifications import (
    build_heartbeat_message,
    build_initialized_message,
    build_running_message,
    build_starting_message,
)
from polymarket_insider_tracker.config import Settings, get_settings
from polymarket_insider_tracker.detector.conviction import ConvictionDetector
from polymarket_insider_tracker.detector.fresh_wallet import FreshWalletDetector
from polymarket_insider_tracker.detector.multi_market import MultiMarketDetector
from polymarket_insider_tracker.detector.scorer import RiskScorer, SignalBundle
from polymarket_insider_tracker.detector.size_anomaly import SizeAnomalyDetector
from polymarket_insider_tracker.detector.timing import TimingDetector
from polymarket_insider_tracker.detector.wallet_cluster import WalletClusterDetector
from polymarket_insider_tracker.detector.whale_tracker import WhaleTracker
from polymarket_insider_tracker.ingestor.clob_client import ClobClient
from polymarket_insider_tracker.ingestor.health import HealthMonitor
from polymarket_insider_tracker.ingestor.market_stats import MarketStatsAggregator
from polymarket_insider_tracker.ingestor.metadata_sync import MarketMetadataSync
from polymarket_insider_tracker.ingestor.websocket import TradeStreamHandler
from polymarket_insider_tracker.profiler.analyzer import WalletAnalyzer
from polymarket_insider_tracker.profiler.chain import PolygonClient
from polymarket_insider_tracker.storage.database import DatabaseManager

if TYPE_CHECKING:
    from typing import Any

    from polymarket_insider_tracker.detector.models import (
        ConvictionSignal,
        FreshWalletSignal,
        MultiMarketSignal,
        SizeAnomalySignal,
        SniperClusterSignal,
        TimingSignal,
        WhaleSignal,
    )
    from polymarket_insider_tracker.ingestor.models import TradeEvent

logger = logging.getLogger(__name__)


class PipelineState(StrEnum):
    """Pipeline lifecycle states."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class PipelineStats:
    """Statistics for the pipeline."""

    started_at: datetime | None = None
    trades_processed: int = 0
    signals_generated: int = 0
    alerts_sent: int = 0
    errors: int = 0
    last_trade_time: datetime | None = None
    last_error: str | None = None


class Pipeline:
    """Main pipeline orchestrator for the Polymarket Insider Tracker.

    This class wires together all detection components and manages the
    event flow from trade ingestion through profiling, detection, and alerting.

    Pipeline flow:
        WebSocket Trade Stream → Wallet Profiler → Detectors → Risk Scorer → Alerter

    Example:
        ```python
        from polymarket_insider_tracker.config import get_settings
        from polymarket_insider_tracker.pipeline import Pipeline

        settings = get_settings()
        pipeline = Pipeline(settings)

        await pipeline.start()
        # Pipeline runs until stop() is called
        await pipeline.stop()
        ```
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dry_run: bool | None = None,
    ) -> None:
        """Initialize the pipeline.

        Args:
            settings: Application settings. If not provided, uses get_settings().
            dry_run: If True, skip sending alerts. Overrides settings.dry_run.
        """
        self._settings = settings or get_settings()
        self._dry_run = dry_run if dry_run is not None else self._settings.dry_run

        self._state = PipelineState.STOPPED
        self._stats = PipelineStats()

        # Components (initialized in start())
        self._redis: Redis | None = None
        self._db_manager: DatabaseManager | None = None
        self._polygon_client: PolygonClient | None = None
        self._clob_client: ClobClient | None = None
        self._metadata_sync: MarketMetadataSync | None = None
        self._wallet_analyzer: WalletAnalyzer | None = None
        self._market_stats: MarketStatsAggregator | None = None
        self._fresh_wallet_detector: FreshWalletDetector | None = None
        self._size_anomaly_detector: SizeAnomalyDetector | None = None
        self._conviction_detector: ConvictionDetector | None = None
        self._timing_detector: TimingDetector | None = None
        self._wallet_cluster_detector: WalletClusterDetector | None = None
        self._multi_market_detector: MultiMarketDetector | None = None
        self._whale_tracker: WhaleTracker | None = None
        self._risk_scorer: RiskScorer | None = None
        self._alert_formatter: AlertFormatter | None = None
        self._alert_dispatcher: AlertDispatcher | None = None
        self._trade_stream: TradeStreamHandler | None = None
        self._health_monitor: HealthMonitor | None = None

        # Synchronization
        self._stop_event: asyncio.Event | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> PipelineState:
        """Current pipeline state."""
        return self._state

    @property
    def stats(self) -> PipelineStats:
        """Current pipeline statistics."""
        return self._stats

    @property
    def is_running(self) -> bool:
        """Check if pipeline is running."""
        return self._state == PipelineState.RUNNING

    async def start(self) -> None:
        """Start the pipeline.

        Initializes all components and begins processing trades.

        Raises:
            RuntimeError: If pipeline is already running.
            Exception: If any component fails to initialize.
        """
        if self._state != PipelineState.STOPPED:
            raise RuntimeError(f"Cannot start pipeline in state {self._state}")

        self._state = PipelineState.STARTING
        self._stop_event = asyncio.Event()
        logger.info("Starting pipeline...")

        try:
            # Set up alerting first so we can send startup notifications
            self._initialize_alerting()
            await self._notify(build_starting_message())

            await self._initialize_components()
            await self._notify(build_initialized_message())

            await self._start_background_services()
            self._stats.started_at = datetime.now(UTC)
            self._state = PipelineState.RUNNING
            logger.info("Pipeline started successfully")
            await self._notify(build_running_message())
        except Exception as e:
            self._state = PipelineState.ERROR
            self._stats.last_error = str(e)
            logger.error("Failed to start pipeline: %s", e)
            await self._cleanup()
            raise

    async def stop(self) -> None:
        """Stop the pipeline gracefully.

        Stops all background services and cleans up resources.
        """
        if self._state == PipelineState.STOPPED:
            return

        self._state = PipelineState.STOPPING
        logger.info("Stopping pipeline...")

        if self._stop_event:
            self._stop_event.set()

        await self._stop_background_services()
        await self._cleanup()

        self._state = PipelineState.STOPPED
        logger.info("Pipeline stopped")

    async def _initialize_components(self) -> None:
        """Initialize all pipeline components."""
        settings = self._settings

        # Initialize Health Monitor
        # Do this first so that the logging captures the following logs
        logger.info("Initializing health monitor...")
        self._health_monitor = HealthMonitor(get_pipeline_stats=self._get_health_stats)

        # Attach in-memory log handler to root logger so /logs endpoint works
        root_logger = logging.getLogger()
        root_logger.addHandler(self._health_monitor._log_handler)

        # Initialize Redis
        logger.debug("Initializing Redis connection...")
        self._redis = Redis.from_url(settings.redis.url)

        # Initialize Database Manager
        logger.debug("Initializing database manager...")
        self._db_manager = DatabaseManager(
            settings.database.url,
            async_mode=True,
        )

        # Initialize Polygon client
        logger.debug("Initializing Polygon client...")
        self._polygon_client = PolygonClient(
            settings.polygon.rpc_url,
            fallback_rpc_url=settings.polygon.fallback_rpc_url,
            redis=self._redis,
        )

        # Initialize CLOB client
        logger.debug("Initializing CLOB client...")
        api_key = (
            settings.polymarket.api_key.get_secret_value() if settings.polymarket.api_key else None
        )
        self._clob_client = ClobClient(api_key=api_key)

        # Initialize Market Metadata Sync
        logger.info("Initializing market metadata sync...")
        self._metadata_sync = MarketMetadataSync(
            redis=self._redis,
            clob_client=self._clob_client,
        )

        # Initialize Wallet Analyzer
        logger.info("Initializing wallet analyzer...")
        self._wallet_analyzer = WalletAnalyzer(
            self._polygon_client,
            redis=self._redis,
        )

        # Initialize rolling market stats aggregator
        logger.info("Initializing market stats aggregator...")
        self._market_stats = MarketStatsAggregator(redis=self._redis)

        # Initialize Detectors
        logger.info("Initializing detectors...")
        self._fresh_wallet_detector = FreshWalletDetector(self._wallet_analyzer)
        self._size_anomaly_detector = SizeAnomalyDetector(
            self._metadata_sync, market_stats=self._market_stats
        )
        self._conviction_detector = ConvictionDetector()
        self._timing_detector = TimingDetector(self._metadata_sync)
        self._wallet_cluster_detector = WalletClusterDetector(
            self._redis, self._wallet_analyzer
        )
        self._multi_market_detector = MultiMarketDetector(self._redis)
        self._whale_tracker = WhaleTracker(self._redis)

        # Initialize Risk Scorer
        logger.info("Initializing risk scorer...")
        self._risk_scorer = RiskScorer(self._redis)

        # Initialize Trade Stream
        logger.info("Initializing trade stream handler...")
        self._trade_stream = TradeStreamHandler(
            on_trade=self._on_trade,
            host=settings.polymarket.ws_url,
        )

        logger.info("All components initialized")

    def _get_health_stats(self) -> dict[str, Any]:
        """Return pipeline stats for the /health endpoint."""
        stats = self._stats
        return {
            "state": self._state.value,
            "trades_processed": stats.trades_processed,
            "signals_generated": stats.signals_generated,
            "alerts_sent": stats.alerts_sent,
            "errors": stats.errors,
            "last_trade_time": stats.last_trade_time.isoformat() if stats.last_trade_time else None,
            "last_error": stats.last_error,
        }

    async def _notify(self, msg: FormattedAlert) -> None:
        """Send a system notification to all configured channels."""
        if not self._alert_dispatcher or not self._alert_dispatcher.channels:
            return
        if self._dry_run:
            logger.info("[DRY RUN] Would send notification: %s", msg.title)
            return
        try:
            result = await self._alert_dispatcher.dispatch(msg)
            if result.all_succeeded:
                logger.info("Notification sent: %s", msg.title)
            else:
                logger.warning("Notification partially failed: %s", msg.title)
        except Exception as e:
            logger.error("Failed to send notification '%s': %s", msg.title, e)

    async def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat notifications during configured hours."""
        interval = self._settings.heartbeat_interval_minutes * 60
        start_hour = self._settings.heartbeat_start_hour
        end_hour = self._settings.heartbeat_end_hour

        while True:
            try:
                await asyncio.sleep(interval)

                # Check if current server time is within the notification window
                now = datetime.now()
                if start_hour <= end_hour:
                    in_window = start_hour <= now.hour < end_hour
                else:
                    # Wraps midnight (e.g. 21-09)
                    in_window = now.hour >= start_hour or now.hour < end_hour

                if not in_window:
                    logger.debug("Heartbeat skipped - outside notification window (%d:00-%d:00)", start_hour, end_hour)
                    continue

                if self._dry_run:
                    logger.info("[DRY RUN] Would send heartbeat notification")
                    continue

                if not self._alert_dispatcher or not self._health_monitor:
                    continue

                # Get health data from the monitor
                report = self._health_monitor.get_health_report()
                health_data: dict[str, Any] = {
                    "status": report.status.value,
                    "streams": {},
                }
                for name, stream in report.streams.items():
                    health_data["streams"][name] = {
                        "status": stream.status.value,
                        "events_received": stream.events_received,
                        "events_per_second": round(stream.events_per_second, 2),
                    }
                health_data["pipeline"] = self._get_health_stats()

                msg = build_heartbeat_message(
                    health_data,
                    report.uptime_seconds,
                )
                result = await self._alert_dispatcher.dispatch(msg)
                if result.all_succeeded:
                    logger.info("Heartbeat notification sent")
                else:
                    logger.warning("Heartbeat notification partially failed")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in heartbeat loop: %s", e)
                await asyncio.sleep(60)

    def _initialize_alerting(self) -> None:
        """Initialize alert channels and dispatcher (must run before notifications)."""
        logger.info("Initializing alerting components...")
        self._alert_formatter = AlertFormatter(verbosity="detailed")
        channels = self._build_alert_channels()
        self._alert_dispatcher = AlertDispatcher(channels)

    def _build_alert_channels(self) -> list[AlertChannel]:
        """Build list of enabled alert channels."""
        channels: list[AlertChannel] = []
        settings = self._settings

        if settings.discord.enabled and settings.discord.webhook_url:
            webhook_url = settings.discord.webhook_url.get_secret_value()
            channels.append(DiscordChannel(webhook_url))
            logger.info("Discord channel enabled")

        if settings.telegram.enabled:
            bot_token = settings.telegram.bot_token
            chat_id = settings.telegram.chat_id
            if bot_token and chat_id:
                channels.append(
                    TelegramChannel(
                        bot_token.get_secret_value(),
                        chat_id,
                    )
                )
                logger.info("Telegram channel enabled")

        if not channels:
            logger.warning("No alert channels configured")

        return channels

    async def _start_background_services(self) -> None:
        """Start background services."""
        # Start health monitor and HTTP server
        if self._health_monitor:
            await self._health_monitor.start()
            await self._health_monitor.start_http_server(self._settings.health_port)

        # Start metadata sync
        if self._metadata_sync:
            logger.debug("Starting metadata sync service...")
            await self._metadata_sync.start()

        # Start trade stream in background task
        if self._trade_stream:
            logger.debug("Starting trade stream...")
            self._stream_task = asyncio.create_task(self._run_trade_stream())

        # Start heartbeat task
        if self._settings.heartbeat_interval_minutes > 0 and self._alert_dispatcher:
            logger.debug("Starting heartbeat task (every %dm)...", self._settings.heartbeat_interval_minutes)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _run_trade_stream(self) -> None:
        """Run the trade stream in a task."""
        if not self._trade_stream:
            return

        try:
            await self._trade_stream.start()
        except asyncio.CancelledError:
            logger.debug("Trade stream task cancelled")
        except Exception as e:
            logger.error("Trade stream error: %s", e)
            self._stats.last_error = str(e)
            self._stats.errors += 1

    async def _stop_background_services(self) -> None:
        """Stop background services."""
        # Stop trade stream
        if self._trade_stream:
            logger.debug("Stopping trade stream...")
            await self._trade_stream.stop()

        # Cancel stream task
        if self._stream_task:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None

        # Cancel heartbeat task
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        # Stop metadata sync
        if self._metadata_sync:
            logger.debug("Stopping metadata sync...")
            await self._metadata_sync.stop()

    async def _cleanup(self) -> None:
        """Clean up resources."""
        # Stop health monitor
        if self._health_monitor:
            logging.getLogger().removeHandler(self._health_monitor._log_handler)
            await self._health_monitor.stop()
            self._health_monitor = None

        # Close database connections
        if self._db_manager:
            await self._db_manager.dispose_async()
            self._db_manager = None

        # Close Redis connection
        if self._redis:
            await self._redis.aclose()
            self._redis = None

        logger.debug("Resources cleaned up")

    async def _on_trade(self, trade: TradeEvent) -> None:
        """Process a single trade event.

        This is the main event handler that runs the detection pipeline:
        1. Run fresh wallet detection
        2. Run size anomaly detection
        3. Score the combined signals
        4. Send alert if threshold exceeded

        Args:
            trade: The trade event from the WebSocket stream.
        """
        self._stats.trades_processed += 1
        self._stats.last_trade_time = datetime.now(UTC)

        try:
            # Record trade in rolling market stats (before detectors so stats are current)
            if self._market_stats:
                await self._market_stats.record_trade(
                    trade.market_id,
                    trade.wallet_address,
                    trade.notional_value,
                    trade.trade_id,
                )

            # Run all detectors in parallel
            (
                fresh_signal,
                size_signal,
                cluster_signal,
                conviction_signal,
                timing_signal,
                multi_market_signal,
                whale_signal,
            ) = await asyncio.gather(
                self._detect_fresh_wallet(trade),
                self._detect_size_anomaly(trade),
                self._detect_wallet_cluster(trade),
                self._detect_conviction(trade),
                self._detect_timing(trade),
                self._detect_multi_market(trade),
                self._detect_whale(trade),
            )

            # Bundle signals
            bundle = SignalBundle(
                trade_event=trade,
                fresh_wallet_signal=fresh_signal,
                size_anomaly_signal=size_signal,
                sniper_cluster_signal=cluster_signal,
                conviction_signal=conviction_signal,
                timing_signal=timing_signal,
                multi_market_signal=multi_market_signal,
                whale_signal=whale_signal,
            )

            # Score and potentially alert if any signal fired
            has_signal = any([
                fresh_signal, size_signal, cluster_signal,
                conviction_signal, timing_signal,
                multi_market_signal, whale_signal,
            ])
            if has_signal:
                self._stats.signals_generated += 1
                await self._score_and_alert(bundle)

        except Exception as e:
            logger.error("Error processing trade %s: %s", trade.trade_id, e)
            self._stats.errors += 1
            self._stats.last_error = str(e)

    async def _detect_fresh_wallet(self, trade: TradeEvent) -> FreshWalletSignal | None:
        """Run fresh wallet detection."""
        if not self._fresh_wallet_detector:
            return None
        try:
            return await self._fresh_wallet_detector.analyze(trade)
        except Exception as e:
            logger.warning("Fresh wallet detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _detect_size_anomaly(self, trade: TradeEvent) -> SizeAnomalySignal | None:
        """Run size anomaly detection."""
        if not self._size_anomaly_detector:
            return None
        try:
            return await self._size_anomaly_detector.analyze(trade)
        except Exception as e:
            logger.warning("Size anomaly detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _detect_wallet_cluster(self, trade: TradeEvent) -> SniperClusterSignal | None:
        if not self._wallet_cluster_detector:
            return None
        try:
            return await self._wallet_cluster_detector.analyze(trade)
        except Exception as e:
            logger.warning("Wallet cluster detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _detect_conviction(self, trade: TradeEvent) -> ConvictionSignal | None:
        if not self._conviction_detector:
            return None
        try:
            return await self._conviction_detector.analyze(trade)
        except Exception as e:
            logger.warning("Conviction detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _detect_timing(self, trade: TradeEvent) -> TimingSignal | None:
        if not self._timing_detector:
            return None
        try:
            return await self._timing_detector.analyze(trade)
        except Exception as e:
            logger.warning("Timing detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _detect_multi_market(self, trade: TradeEvent) -> MultiMarketSignal | None:
        if not self._multi_market_detector:
            return None
        try:
            return await self._multi_market_detector.analyze(trade)
        except Exception as e:
            logger.warning("Multi-market detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _detect_whale(self, trade: TradeEvent) -> WhaleSignal | None:
        if not self._whale_tracker:
            return None
        try:
            return await self._whale_tracker.analyze(trade)
        except Exception as e:
            logger.warning("Whale detection failed for %s: %s", trade.trade_id, e)
            return None

    async def _score_and_alert(self, bundle: SignalBundle) -> None:
        """Score signals and send alert if threshold exceeded."""
        if not self._risk_scorer or not self._alert_formatter or not self._alert_dispatcher:
            return

        # Get risk assessment
        assessment = await self._risk_scorer.assess(bundle)

        if not assessment.should_alert:
            logger.debug(
                "Trade %s below alert threshold (score=%.2f)",
                bundle.trade_event.trade_id,
                assessment.weighted_score,
            )
            return

        # Format and dispatch alert
        formatted_alert = self._alert_formatter.format(assessment)

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would send alert: wallet=%s, score=%.2f",
                assessment.wallet_address[:10] + "...",
                assessment.weighted_score,
            )
            return

        result = await self._alert_dispatcher.dispatch(formatted_alert)

        if result.all_succeeded:
            self._stats.alerts_sent += 1
            logger.info(
                "Alert sent successfully: wallet=%s, score=%.2f",
                assessment.wallet_address[:10] + "...",
                assessment.weighted_score,
            )
        else:
            logger.warning(
                "Alert partially failed: %d/%d channels succeeded",
                result.success_count,
                result.success_count + result.failure_count,
            )

    async def run(self) -> None:
        """Start the pipeline and run until interrupted.

        This is a convenience method that starts the pipeline and
        blocks until a stop signal is received.

        Example:
            ```python
            pipeline = Pipeline()
            try:
                await pipeline.run()
            except KeyboardInterrupt:
                pass
            ```
        """
        await self.start()

        try:
            if self._stop_event:
                await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def __aenter__(self) -> Pipeline:
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.stop()
