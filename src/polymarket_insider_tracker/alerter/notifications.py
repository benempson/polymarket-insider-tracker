"""System notifications (startup, heartbeat) for bot channels."""

from __future__ import annotations

import os
import re
from datetime import datetime

from polymarket_insider_tracker.alerter.models import FormattedAlert

_TELEGRAM_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def _esc(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    return _TELEGRAM_SPECIAL.sub(r"\\\1", str(text))


def _get_git_sha() -> str:
    """Return short git SHA baked in at Docker build time, or 'dev'."""
    sha = os.environ.get("GIT_SHA", "dev")
    return sha if sha and sha != "unknown" else "dev"


def _build_phase_message(phase: str, description: str, color: int) -> FormattedAlert:
    """Build a startup phase notification."""
    sha = _get_git_sha()
    ts = datetime.now(tz=None).strftime("%Y-%m-%d %H:%M:%S")

    title = f"Tracker - {phase}"
    body = f"[{sha}] {phase}: {description}"

    discord_embed = {
        "title": f"Polymarket Insider Tracker - {phase}",
        "description": f"**Version:** `{sha}`\n{description}",
        "color": color,
        "footer": {"text": ts},
    }

    telegram_md = (
        f"*Polymarket Insider Tracker \\- {_esc(phase)}*\n\n"
        f"*Version:* `{_esc(sha)}`\n"
        f"{_esc(description)}"
    )

    plain = f"[{sha}] {phase}: {description}"

    return FormattedAlert(
        title=title,
        body=body,
        discord_embed=discord_embed,
        telegram_markdown=telegram_md,
        plain_text=plain,
    )


def build_starting_message() -> FormattedAlert:
    """Phase 1: Pipeline is beginning startup."""
    return _build_phase_message(
        "Starting",
        "Initializing components...",
        0x3498DB,  # blue
    )


def build_initialized_message() -> FormattedAlert:
    """Phase 2: All components initialized successfully."""
    return _build_phase_message(
        "Initialized",
        "All components initialized. Starting background services...",
        0xF39C12,  # amber
    )


def build_running_message() -> FormattedAlert:
    """Phase 3: Pipeline is fully running."""
    return _build_phase_message(
        "Running",
        "Pipeline is live and processing trades.",
        0x2ECC71,  # green
    )


def build_heartbeat_message(
    health_data: dict,
    uptime_seconds: float,
) -> FormattedAlert:
    """Build a heartbeat notification with health info."""
    sha = _get_git_sha()
    now_str = datetime.now(tz=None).strftime("%Y-%m-%d %H:%M:%S")

    # Format uptime
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m"

    # Extract pipeline stats
    pipeline = health_data.get("pipeline", {})
    state = pipeline.get("state", "unknown")
    trades = pipeline.get("trades_processed", 0)
    signals = pipeline.get("signals_generated", 0)
    alerts = pipeline.get("alerts_sent", 0)
    errors = pipeline.get("errors", 0)
    last_trade = pipeline.get("last_trade_time")
    last_error = pipeline.get("last_error")

    # Extract stream info
    streams = health_data.get("streams", {})
    stream_lines_discord = []
    stream_lines_telegram = []
    for name, info in streams.items():
        s_status = info.get("status", "unknown")
        s_events = info.get("events_received", 0)
        s_eps = info.get("events_per_second", 0)
        stream_lines_discord.append(
            f"  {name}: {s_status} ({s_events} events, {s_eps}/s)"
        )
        stream_lines_telegram.append(
            f"  {_esc(name)}: {_esc(s_status)} \\({_esc(str(s_events))} events, {_esc(str(s_eps))}/s\\)"
        )

    overall_status = health_data.get("status", "unknown")

    title = "Heartbeat"
    body = f"Status: {overall_status}, Uptime: {uptime_str}, Trades: {trades}"

    # Discord embed
    desc_parts = [
        f"**Status:** {overall_status}",
        f"**Version:** `{sha}`",
        f"**Uptime:** {uptime_str}",
        f"**Pipeline:** {state}",
        "",
        f"**Trades processed:** {trades:,}",
        f"**Signals generated:** {signals:,}",
        f"**Alerts sent:** {alerts:,}",
        f"**Errors:** {errors:,}",
    ]
    if last_trade:
        desc_parts.append(f"**Last trade:** {last_trade}")
    if last_error:
        desc_parts.append(f"**Last error:** {last_error}")
    if stream_lines_discord:
        desc_parts.append("")
        desc_parts.append("**Streams:**")
        desc_parts.extend(stream_lines_discord)

    color = 0x2ECC71 if overall_status == "healthy" else 0xE67E22 if overall_status == "degraded" else 0xE74C3C

    discord_embed = {
        "title": "Polymarket Insider Tracker - Heartbeat",
        "description": "\n".join(desc_parts),
        "color": color,
        "footer": {"text": now_str},
    }

    # Telegram markdown
    tg_parts = [
        "*Polymarket Insider Tracker \\- Heartbeat*\n",
        f"*Status:* {_esc(overall_status)}",
        f"*Version:* `{_esc(sha)}`",
        f"*Uptime:* {_esc(uptime_str)}",
        f"*Pipeline:* {_esc(state)}",
        "",
        f"*Trades processed:* {_esc(f'{trades:,}')}",
        f"*Signals generated:* {_esc(f'{signals:,}')}",
        f"*Alerts sent:* {_esc(f'{alerts:,}')}",
        f"*Errors:* {_esc(f'{errors:,}')}",
    ]
    if last_trade:
        tg_parts.append(f"*Last trade:* {_esc(last_trade)}")
    if last_error:
        tg_parts.append(f"*Last error:* {_esc(last_error)}")
    if stream_lines_telegram:
        tg_parts.append("")
        tg_parts.append("*Streams:*")
        tg_parts.extend(stream_lines_telegram)

    telegram_md = "\n".join(tg_parts)

    plain = (
        f"Heartbeat: status={overall_status}, uptime={uptime_str}, "
        f"trades={trades}, signals={signals}, alerts={alerts}, errors={errors}"
    )

    return FormattedAlert(
        title=title,
        body=body,
        discord_embed=discord_embed,
        telegram_markdown=telegram_md,
        plain_text=plain,
    )
