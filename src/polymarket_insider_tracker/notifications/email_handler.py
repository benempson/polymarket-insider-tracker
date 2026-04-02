"""SMTP error email handler with deduplication.

Sends email notifications for ERROR+ log records with in-memory
deduplication to prevent email storms from recurring errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import smtplib
import threading
import time
from email.mime.text import MIMEText

# Use a separate logger to avoid recursion when email sending fails
_internal_logger = logging.getLogger("email_handler")


class SmtpErrorHandler(logging.Handler):
    """Logging handler that emails ERROR+ log records with deduplication.

    Deduplicates by hashing the formatted log message — the same error
    message is only emailed once per cooldown period. Sends via stdlib
    smtplib in a thread pool to avoid blocking the async event loop.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        from_address: str,
        to_addresses: list[str],
        *,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        cooldown_seconds: int = 1800,
        subject_prefix: str = "[Polymarket Tracker]",
        timeout: float = 10.0,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._from_address = from_address
        self._to_addresses = to_addresses
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._cooldown_seconds = cooldown_seconds
        self._subject_prefix = subject_prefix
        self._timeout = timeout

        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for async email sending.

        Must be called after the asyncio loop is running.
        """
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        """Process a log record — send email if not deduplicated."""
        if record.levelno < self.level:
            return
        try:
            msg_hash = hashlib.md5(  # noqa: S324
                record.getMessage().encode()
            ).hexdigest()
            now = time.time()

            with self._lock:
                last_sent = self._cooldown.get(msg_hash, 0.0)
                if now - last_sent < self._cooldown_seconds:
                    return
                self._cooldown[msg_hash] = now
                self._prune_cooldown(now)

            subject = (
                f"{self._subject_prefix} {record.levelname}: "
                f"{record.getMessage()[:80]}"
            )
            body = self.format(record)

            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    self._loop.create_task,
                    self._send_async(subject, body),
                )
            else:
                self._send_sync(subject, body)
        except Exception:
            self.handleError(record)

    async def _send_async(self, subject: str, body: str) -> None:
        """Send email asynchronously via thread pool."""
        try:
            await asyncio.to_thread(self._send_sync, subject, body)
        except Exception as e:
            _internal_logger.warning("Failed to send error email: %s", e)

    def _send_sync(self, subject: str, body: str) -> None:
        """Send email synchronously via SMTP."""
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self._from_address
        msg["To"] = ", ".join(self._to_addresses)

        with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=self._timeout) as server:
            if self._use_tls:
                server.starttls()
            if self._username and self._password:
                server.login(self._username, self._password)
            server.send_message(msg)

    def send_test_email(self) -> bool:
        """Send a test email to verify SMTP connectivity.

        Returns:
            True if the test email was sent successfully.
        """
        subject = f"{self._subject_prefix} Startup: email notifications active"
        body = (
            "This is a test email from Polymarket Insider Tracker.\n\n"
            "Email error notifications are now active. You will receive "
            "emails when ERROR-level log messages occur, with deduplication "
            f"to avoid storms (cooldown: {self._cooldown_seconds // 60} minutes).\n\n"
            f"Recipients: {', '.join(self._to_addresses)}"
        )
        try:
            self._send_sync(subject, body)
            return True
        except Exception as e:
            _internal_logger.warning("Test email failed: %s", e)
            return False

    def _prune_cooldown(self, now: float) -> None:
        """Remove expired entries from the cooldown dict.

        Must be called while holding self._lock.
        """
        cutoff = now - (self._cooldown_seconds * 2)
        self._cooldown = {k: v for k, v in self._cooldown.items() if v > cutoff}


def create_email_handler(
    smtp_host: str,
    smtp_port: int,
    from_address: str,
    to_addresses: list[str],
    *,
    username: str | None = None,
    password: str | None = None,
    use_tls: bool = True,
    cooldown_seconds: int = 1800,
) -> SmtpErrorHandler:
    """Create a configured SmtpErrorHandler.

    Args:
        smtp_host: SMTP server hostname.
        smtp_port: SMTP server port.
        from_address: Sender email address.
        to_addresses: List of recipient email addresses.
        username: SMTP auth username.
        password: SMTP auth password.
        use_tls: Whether to use STARTTLS.
        cooldown_seconds: Deduplication cooldown in seconds.

    Returns:
        Configured SmtpErrorHandler instance.
    """
    return SmtpErrorHandler(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        from_address=from_address,
        to_addresses=to_addresses,
        username=username,
        password=password,
        use_tls=use_tls,
        cooldown_seconds=cooldown_seconds,
    )
