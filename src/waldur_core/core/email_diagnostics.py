"""Sanity checks for the outgoing email configuration.

Waldur never sets ``EMAIL_BACKEND`` or ``EMAIL_HOST`` itself: an install that
was never given an ``override.conf.py`` silently talks to Django's default
``localhost:25`` and every notification fails at send time. On top of that,
a perfectly good relay still sends nothing while every ``Notification`` row is
disabled, and that combination logs nothing at all to explain itself.

This module turns both halves into an explicit report. The audit reads
settings only — no socket is opened, so it is safe to call on every page load.
``probe_smtp`` is the part that connects, and it is deliberately a separate
call behind its own button.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import get_connection
from django.core.validators import validate_email
from django.utils import timezone

from waldur_core.core.models import Notification
from waldur_core.logging.models import EmailLog

logger = logging.getLogger(__name__)

OK = "OK"
WARNING = "WARNING"
ERROR = "ERROR"

# Hosts that mean "nobody has configured a relay yet". `waldur-smtp` is the
# mastermind image's own placeholder in docker/rootfs/etc/waldur/override.conf.py,
# which a real deployment overwrites with a mounted override.
PLACEHOLDER_HOSTS = {
    "waldur-smtp",
    "smtp.example.com",
    "mail.example.com",
    "example.com",
}

# Django's default host. Legitimate only when a relay runs in the same pod or
# on the same host, so it is a warning rather than an error — the probe settles it.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

PLACEHOLDER_ADDRESSES = {
    "webmaster@localhost",
    "noreply@example.com",
    "support@example.com",
}

SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# Backends that accept mail and never deliver it to a real recipient.
NON_DELIVERING_BACKENDS = {
    "django.core.mail.backends.console.EmailBackend": "printed to the container log",
    "django.core.mail.backends.dummy.EmailBackend": "discarded",
    "django.core.mail.backends.locmem.EmailBackend": "kept in memory",
    "django.core.mail.backends.filebased.EmailBackend": "written to a local directory",
}

DEFAULT_PROBE_TIMEOUT = 10


@dataclass
class Finding:
    level: str
    code: str
    title: str
    detail: str = ""
    remediation: str = ""


@dataclass
class EmailConfig:
    """Effective mail settings, with the password reduced to a boolean."""

    backend: str
    host: str
    port: int | None
    host_user: str
    has_password: bool
    use_tls: bool
    use_ssl: bool
    timeout: int | None
    default_from_email: str
    default_reply_to_email: str
    subject_prefix: str


@dataclass
class EmailDiagnostics:
    config: EmailConfig
    findings: list[Finding] = field(default_factory=list)
    enabled_notification_count: int = 0
    total_notification_count: int = 0
    emails_sent_last_week: int = 0
    last_email_sent_at: object = None

    @property
    def status(self) -> str:
        if any(f.level == ERROR for f in self.findings):
            return ERROR
        if any(f.level == WARNING for f in self.findings):
            return WARNING
        return OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "config": asdict(self.config),
            "findings": [asdict(f) for f in self.findings],
            "enabled_notification_count": self.enabled_notification_count,
            "total_notification_count": self.total_notification_count,
            "emails_sent_last_week": self.emails_sent_last_week,
            "last_email_sent_at": self.last_email_sent_at,
        }


def get_email_config() -> EmailConfig:
    return EmailConfig(
        backend=settings.EMAIL_BACKEND,
        host=settings.EMAIL_HOST,
        port=settings.EMAIL_PORT,
        host_user=settings.EMAIL_HOST_USER,
        has_password=bool(settings.EMAIL_HOST_PASSWORD),
        use_tls=bool(settings.EMAIL_USE_TLS),
        use_ssl=bool(settings.EMAIL_USE_SSL),
        timeout=settings.EMAIL_TIMEOUT,
        default_from_email=settings.DEFAULT_FROM_EMAIL,
        default_reply_to_email=getattr(settings, "DEFAULT_REPLY_TO_EMAIL", ""),
        subject_prefix=settings.EMAIL_SUBJECT_PREFIX,
    )


def _is_valid_email(value: str) -> bool:
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


def _check_backend(config: EmailConfig, findings: list[Finding]) -> None:
    if config.backend in NON_DELIVERING_BACKENDS:
        findings.append(
            Finding(
                level=ERROR,
                code="backend_does_not_deliver",
                title="Mail is not delivered to recipients",
                detail=(
                    f"EMAIL_BACKEND is {config.backend}, so every message is "
                    f"{NON_DELIVERING_BACKENDS[config.backend]} instead of being handed to a relay."
                ),
                remediation=(
                    f"Set EMAIL_BACKEND to {SMTP_BACKEND} in override.conf.py, "
                    "or remove the override to fall back to Django's default."
                ),
            )
        )
    elif config.backend != SMTP_BACKEND:
        findings.append(
            Finding(
                level=WARNING,
                code="custom_backend",
                title="Custom email backend in use",
                detail=(
                    f"EMAIL_BACKEND is {config.backend}. The checks below describe the "
                    "standard SMTP settings, which a custom backend may ignore."
                ),
            )
        )


def _check_host(config: EmailConfig, findings: list[Finding]) -> None:
    host = config.host.strip()
    if not host:
        findings.append(
            Finding(
                level=ERROR,
                code="host_missing",
                title="No SMTP relay is configured",
                detail="EMAIL_HOST is empty, so no message can leave this installation.",
                remediation=(
                    "Set the relay in override.conf.py — waldur.mail.host in the Helm "
                    "values, or EMAIL_HOST in the docker-compose .env file."
                ),
            )
        )
    elif host in PLACEHOLDER_HOSTS:
        findings.append(
            Finding(
                level=ERROR,
                code="host_is_placeholder",
                title="SMTP relay is still the shipped placeholder",
                detail=(
                    f"EMAIL_HOST is {host!r}, the placeholder baked into the distribution. "
                    "It does not resolve to a real relay."
                ),
                remediation="Point EMAIL_HOST at the relay this deployment should use.",
            )
        )
    elif host in LOCAL_HOSTS:
        findings.append(
            Finding(
                level=WARNING,
                code="host_is_local",
                title="SMTP relay is the local host",
                detail=(
                    f"EMAIL_HOST is {host!r}, which is Django's default. This is correct only "
                    "if a relay runs inside this container or pod; otherwise nothing is sent."
                ),
                remediation="Run the connection test below to confirm a relay answers.",
            )
        )
    else:
        findings.append(
            Finding(
                level=OK,
                code="host_configured",
                title="SMTP relay is configured",
                detail=f"{host}:{config.port}",
            )
        )


def _check_encryption(config: EmailConfig, findings: list[Finding]) -> None:
    if config.use_tls and config.use_ssl:
        findings.append(
            Finding(
                level=ERROR,
                code="tls_and_ssl",
                title="EMAIL_USE_TLS and EMAIL_USE_SSL are both enabled",
                detail=(
                    "They are mutually exclusive and Django raises at send time, so every "
                    "notification fails."
                ),
                remediation=(
                    "Keep EMAIL_USE_TLS with port 587 (STARTTLS) or EMAIL_USE_SSL with "
                    "port 465 (implicit TLS), never both."
                ),
            )
        )
        return

    if not config.use_tls and not config.use_ssl:
        if config.port in (465, 587):
            findings.append(
                Finding(
                    level=WARNING,
                    code="encryption_missing",
                    title="Submission port used without encryption",
                    detail=(
                        f"Port {config.port} is a submission port, but neither EMAIL_USE_TLS "
                        "nor EMAIL_USE_SSL is set. Most relays reject the session."
                    ),
                    remediation="Enable EMAIL_USE_TLS for port 587 or EMAIL_USE_SSL for port 465.",
                )
            )
        else:
            findings.append(
                Finding(
                    level=WARNING,
                    code="encryption_disabled",
                    title="Mail is submitted without encryption",
                    detail=(
                        "Neither EMAIL_USE_TLS nor EMAIL_USE_SSL is set, so credentials and "
                        "message bodies cross the network in the clear."
                    ),
                    remediation=(
                        "Acceptable for a relay reached over a trusted network only; "
                        "otherwise enable EMAIL_USE_TLS on port 587."
                    ),
                )
            )
        return

    if config.use_tls and config.port == 465:
        findings.append(
            Finding(
                level=WARNING,
                code="tls_on_implicit_port",
                title="STARTTLS configured on the implicit-TLS port",
                detail=(
                    "Port 465 expects implicit TLS from the first byte, but EMAIL_USE_TLS "
                    "asks for STARTTLS on a plaintext session."
                ),
                remediation="Use EMAIL_USE_SSL with port 465, or EMAIL_USE_TLS with port 587.",
            )
        )
    elif config.use_ssl and config.port in (25, 587):
        findings.append(
            Finding(
                level=WARNING,
                code="ssl_on_starttls_port",
                title="Implicit TLS configured on a STARTTLS port",
                detail=(
                    f"Port {config.port} expects a plaintext session upgraded with STARTTLS, "
                    "but EMAIL_USE_SSL opens a TLS socket immediately."
                ),
                remediation="Use EMAIL_USE_TLS with port 587, or EMAIL_USE_SSL with port 465.",
            )
        )
    else:
        findings.append(
            Finding(
                level=OK,
                code="encryption_configured",
                title="Transport encryption is enabled",
                detail="STARTTLS" if config.use_tls else "Implicit TLS",
            )
        )


def _check_credentials(config: EmailConfig, findings: list[Finding]) -> None:
    if config.host_user and not config.has_password:
        findings.append(
            Finding(
                level=WARNING,
                code="password_missing",
                title="SMTP user without a password",
                detail=(
                    "EMAIL_HOST_USER is set but EMAIL_HOST_PASSWORD is empty. Django only "
                    "authenticates when both are non-empty, so the session stays anonymous."
                ),
                remediation=(
                    "Set EMAIL_HOST_PASSWORD, or clear EMAIL_HOST_USER if the relay accepts "
                    "unauthenticated mail."
                ),
            )
        )
    elif config.has_password and not config.host_user:
        findings.append(
            Finding(
                level=WARNING,
                code="user_missing",
                title="SMTP password without a user",
                detail=(
                    "EMAIL_HOST_PASSWORD is set but EMAIL_HOST_USER is empty, so the password "
                    "is never used."
                ),
                remediation="Set EMAIL_HOST_USER, or clear the password.",
            )
        )


def _check_timeout(config: EmailConfig, findings: list[Finding]) -> None:
    if config.timeout is None:
        findings.append(
            Finding(
                level=WARNING,
                code="timeout_unset",
                title="No SMTP timeout is set",
                detail=(
                    "EMAIL_TIMEOUT is unset, so a relay that accepts a connection and then "
                    "stops responding blocks the Celery worker sending the notification "
                    "until the OS gives up."
                ),
                remediation="Set EMAIL_TIMEOUT (10 seconds is a reasonable default).",
            )
        )


def _check_addresses(config: EmailConfig, findings: list[Finding]) -> None:
    from_email = config.default_from_email.strip()
    if not from_email:
        findings.append(
            Finding(
                level=ERROR,
                code="from_email_missing",
                title="No sender address is configured",
                detail="DEFAULT_FROM_EMAIL is empty and relays reject mail without a sender.",
                remediation="Set DEFAULT_FROM_EMAIL to an address the relay is allowed to send as.",
            )
        )
    elif not _is_valid_email(from_email):
        findings.append(
            Finding(
                level=ERROR,
                code="from_email_invalid",
                title="Sender address is not a valid email address",
                detail=f"DEFAULT_FROM_EMAIL is {from_email!r}.",
                remediation="Set DEFAULT_FROM_EMAIL to a valid address.",
            )
        )
    elif from_email in PLACEHOLDER_ADDRESSES:
        findings.append(
            Finding(
                level=WARNING,
                code="from_email_is_placeholder",
                title="Sender address is still the shipped default",
                detail=(
                    f"DEFAULT_FROM_EMAIL is {from_email!r}. Relays commonly reject mail from a "
                    "domain they are not authoritative for, and recipients cannot reply to it."
                ),
                remediation="Set DEFAULT_FROM_EMAIL to an address on this deployment's domain.",
            )
        )

    reply_to = config.default_reply_to_email.strip()
    if reply_to and not _is_valid_email(reply_to):
        findings.append(
            Finding(
                level=WARNING,
                code="reply_to_invalid",
                title="Reply-to address is not a valid email address",
                detail=f"DEFAULT_REPLY_TO_EMAIL is {reply_to!r}.",
                remediation="Set DEFAULT_REPLY_TO_EMAIL to a valid address, or leave it empty.",
            )
        )


def _check_notifications(
    diagnostics: EmailDiagnostics, findings: list[Finding]
) -> None:
    if diagnostics.total_notification_count == 0:
        findings.append(
            Finding(
                level=WARNING,
                code="notifications_not_loaded",
                title="No notification types are registered",
                detail=(
                    "The notification catalogue is empty, which usually means the "
                    "load_notifications step has not run on this installation."
                ),
                remediation="Run the load_notifications management command.",
            )
        )
    elif diagnostics.enabled_notification_count == 0:
        findings.append(
            Finding(
                level=ERROR,
                code="notifications_all_disabled",
                title="Every notification type is disabled",
                detail=(
                    f"None of the {diagnostics.total_notification_count} notification types is "
                    "enabled, so a working relay still sends nothing — and nothing is logged "
                    "to explain it."
                ),
                remediation=(
                    "Enable the notifications this deployment should send under "
                    "Administration → Notifications, or list them in notifications.json."
                ),
            )
        )
    else:
        findings.append(
            Finding(
                level=OK,
                code="notifications_enabled",
                title="Notifications are enabled",
                detail=(
                    f"{diagnostics.enabled_notification_count} of "
                    f"{diagnostics.total_notification_count} notification types are enabled."
                ),
            )
        )


def collect_diagnostics() -> EmailDiagnostics:
    """Audit the mail configuration without opening a connection."""
    config = get_email_config()
    diagnostics = EmailDiagnostics(config=config)

    notifications = Notification.objects.all()
    diagnostics.total_notification_count = notifications.count()
    diagnostics.enabled_notification_count = notifications.filter(enabled=True).count()

    week_ago = timezone.now() - timedelta(days=7)
    diagnostics.emails_sent_last_week = EmailLog.objects.filter(
        sent_at__gte=week_ago
    ).count()
    last_email = EmailLog.objects.order_by("-sent_at").first()
    diagnostics.last_email_sent_at = last_email.sent_at if last_email else None

    findings = diagnostics.findings
    _check_backend(config, findings)
    _check_host(config, findings)
    _check_encryption(config, findings)
    _check_credentials(config, findings)
    _check_timeout(config, findings)
    _check_addresses(config, findings)
    _check_notifications(diagnostics, findings)

    return diagnostics


def _close_quietly(connection) -> None:
    try:
        connection.close()
    except Exception:
        logger.info("Failed to close the probed SMTP connection", exc_info=True)


def probe_smtp(timeout: int = DEFAULT_PROBE_TIMEOUT) -> dict:
    """Open and close a connection to the relay without sending anything.

    The timeout is passed explicitly rather than relying on EMAIL_TIMEOUT: an
    installation that never set one is exactly the installation most likely to
    have an unreachable relay, and the request thread must not hang on it.
    """
    config = get_email_config()

    if config.backend != SMTP_BACKEND:
        return {
            "success": False,
            "latency_ms": None,
            "error": (
                f"EMAIL_BACKEND is {config.backend}, which does not connect to a relay. "
                "There is nothing to probe."
            ),
        }

    connection = get_connection(timeout=timeout, fail_silently=False)
    started = time.monotonic()
    try:
        connection.open()
    except Exception as e:
        logger.info(
            "SMTP connection probe to %s:%s failed: %s", config.host, config.port, e
        )
        # Django's SMTP backend assigns self.connection before STARTTLS and
        # before AUTH, so the two most interesting failures leave a live socket
        # behind. Close it rather than waiting for the garbage collector.
        _close_quietly(connection)
        return {
            "success": False,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }
    latency_ms = round((time.monotonic() - started) * 1000)
    _close_quietly(connection)
    return {"success": True, "latency_ms": latency_ms, "error": ""}


def open_connection(timeout: int = DEFAULT_PROBE_TIMEOUT):
    """A mail connection that cannot outlive the request that opened it.

    ``EMAIL_TIMEOUT`` is commonly unset — ``_check_timeout`` warns about exactly
    that — and an installation with a broken relay is the one most likely to
    have left it unset. Anything driven from the diagnostics UI therefore
    carries its own timeout instead of inheriting ``None``.
    """
    return get_connection(timeout=timeout, fail_silently=False)
