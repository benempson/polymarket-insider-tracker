"""Tests for the SMTP error email handler."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from polymarket_insider_tracker.config import EmailSettings
from polymarket_insider_tracker.notifications.email_handler import (
    SmtpErrorHandler,
    create_email_handler,
)


@pytest.fixture
def handler() -> SmtpErrorHandler:
    """Create a handler with short cooldown for testing."""
    h = SmtpErrorHandler(
        smtp_host="smtp.example.com",
        smtp_port=587,
        from_address="from@example.com",
        to_addresses=["to@example.com"],
        username="user",
        password="pass",
        cooldown_seconds=60,
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _make_record(
    message: str, level: int = logging.ERROR
) -> logging.LogRecord:
    """Create a log record for testing."""
    return logging.LogRecord(
        name="test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestSmtpErrorHandler:
    """Tests for SmtpErrorHandler."""

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_emits_on_error(self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler) -> None:
        """Test that ERROR records trigger email sending."""
        record = _make_record("Something broke", logging.ERROR)
        handler.emit(record)

        mock_smtp_cls.assert_called_once()
        server = mock_smtp_cls.return_value.__enter__.return_value
        server.send_message.assert_called_once()

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_emits_on_critical(self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler) -> None:
        """Test that CRITICAL records trigger email sending."""
        record = _make_record("Fatal error", logging.CRITICAL)
        handler.emit(record)

        server = mock_smtp_cls.return_value.__enter__.return_value
        server.send_message.assert_called_once()

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_does_not_emit_below_error(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that WARNING/INFO/DEBUG records do not trigger email."""
        for level in (logging.WARNING, logging.INFO, logging.DEBUG):
            record = _make_record("Not an error", level)
            handler.emit(record)

        mock_smtp_cls.assert_not_called()

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_dedup_within_cooldown(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that the same error is only emailed once within the cooldown."""
        record1 = _make_record("Duplicate error")
        record2 = _make_record("Duplicate error")

        handler.emit(record1)
        handler.emit(record2)

        server = mock_smtp_cls.return_value.__enter__.return_value
        assert server.send_message.call_count == 1

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    @patch("polymarket_insider_tracker.notifications.email_handler.time.time")
    def test_dedup_expires_after_cooldown(
        self, mock_time: MagicMock, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that the same error is emailed again after cooldown expires."""
        now = 1000.0
        mock_time.return_value = now

        handler.emit(_make_record("Error message"))

        # Advance past cooldown (60 seconds for test handler)
        mock_time.return_value = now + 61
        handler.emit(_make_record("Error message"))

        server = mock_smtp_cls.return_value.__enter__.return_value
        assert server.send_message.call_count == 2

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_different_errors_not_deduped(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that different error messages are sent separately."""
        handler.emit(_make_record("Error A"))
        handler.emit(_make_record("Error B"))

        server = mock_smtp_cls.return_value.__enter__.return_value
        assert server.send_message.call_count == 2

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_send_failure_does_not_crash(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that SMTP failures don't crash the handler."""
        mock_smtp_cls.side_effect = OSError("Connection refused")

        # Should not raise
        handler.emit(_make_record("Error with broken SMTP"))

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_email_subject_format(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that the email subject has the correct format."""
        handler.emit(_make_record("Database connection lost"))

        server = mock_smtp_cls.return_value.__enter__.return_value
        msg = server.send_message.call_args[0][0]
        assert msg["Subject"].startswith("[Polymarket Tracker] ERROR:")
        assert "Database connection lost" in msg["Subject"]
        assert msg["From"] == "from@example.com"
        assert msg["To"] == "to@example.com"

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_tls_and_auth(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test that TLS and authentication are used."""
        handler.emit(_make_record("Test error"))

        server = mock_smtp_cls.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user", "pass")

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_no_tls_when_disabled(self, mock_smtp_cls: MagicMock) -> None:
        """Test that TLS is not used when disabled."""
        h = SmtpErrorHandler(
            smtp_host="smtp.example.com",
            smtp_port=25,
            from_address="from@example.com",
            to_addresses=["to@example.com"],
            use_tls=False,
        )
        h.setFormatter(logging.Formatter("%(message)s"))
        h.emit(_make_record("Test"))

        server = mock_smtp_cls.return_value.__enter__.return_value
        server.starttls.assert_not_called()

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_send_test_email_success(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test send_test_email returns True on success."""
        assert handler.send_test_email() is True

        server = mock_smtp_cls.return_value.__enter__.return_value
        msg = server.send_message.call_args[0][0]
        assert "Startup" in msg["Subject"]

    @patch("polymarket_insider_tracker.notifications.email_handler.smtplib.SMTP")
    def test_send_test_email_failure(
        self, mock_smtp_cls: MagicMock, handler: SmtpErrorHandler
    ) -> None:
        """Test send_test_email returns False on failure."""
        mock_smtp_cls.side_effect = OSError("Connection refused")
        assert handler.send_test_email() is False

    def test_cooldown_pruning(self, handler: SmtpErrorHandler) -> None:
        """Test that old cooldown entries are pruned."""
        now = time.time()
        # Manually add old entries
        handler._cooldown = {
            "old_hash": now - 200,  # Older than 2x cooldown (120s)
            "recent_hash": now - 30,  # Within 2x cooldown
        }

        handler._prune_cooldown(now)

        assert "old_hash" not in handler._cooldown
        assert "recent_hash" in handler._cooldown


class TestCreateEmailHandler:
    """Tests for the factory function."""

    def test_creates_handler(self) -> None:
        """Test that factory creates a properly configured handler."""
        handler = create_email_handler(
            smtp_host="smtp.example.com",
            smtp_port=587,
            from_address="from@example.com",
            to_addresses=["to@example.com"],
            username="user",
            password="pass",
            cooldown_seconds=900,
        )
        assert isinstance(handler, SmtpErrorHandler)
        assert handler._smtp_host == "smtp.example.com"
        assert handler._cooldown_seconds == 900


class TestEmailSettings:
    """Tests for EmailSettings config."""

    def test_enabled_when_configured(self) -> None:
        """Test enabled property with full config."""
        settings = EmailSettings(
            smtp_host="smtp.example.com",
            from_address="from@example.com",
            to_addresses="to@example.com",
        )
        assert settings.enabled is True

    def test_disabled_when_no_host(self) -> None:
        """Test disabled when smtp_host is missing."""
        settings = EmailSettings(
            from_address="from@example.com",
            to_addresses="to@example.com",
        )
        assert settings.enabled is False

    def test_disabled_when_no_from(self) -> None:
        """Test disabled when from_address is missing."""
        settings = EmailSettings(
            smtp_host="smtp.example.com",
            to_addresses="to@example.com",
        )
        assert settings.enabled is False

    def test_disabled_when_no_to(self) -> None:
        """Test disabled when to_addresses is missing."""
        settings = EmailSettings(
            smtp_host="smtp.example.com",
            from_address="from@example.com",
        )
        assert settings.enabled is False

    def test_recipients_parsing(self) -> None:
        """Test comma-separated recipient parsing."""
        settings = EmailSettings(
            smtp_host="smtp.example.com",
            from_address="from@example.com",
            to_addresses="a@x.com, b@x.com, c@x.com",
        )
        assert settings.recipients == ["a@x.com", "b@x.com", "c@x.com"]

    def test_recipients_empty_when_not_set(self) -> None:
        """Test empty recipients when not configured."""
        settings = EmailSettings()
        assert settings.recipients == []
