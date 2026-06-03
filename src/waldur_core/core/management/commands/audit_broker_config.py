"""Static audit of Celery / RabbitMQ broker configuration.

Inspects Django settings and the live Celery app config for the
common misconfigurations that silently degrade publisher reliability.
No broker connection is opened — safe to run anywhere, including
pre-deploy hooks.

Exit codes:
    0  all checks OK
    1  warnings only
    2  errors found (configuration is likely broken)

For runtime publish-latency measurements that DO connect to the
broker, see ``probe_broker_latency``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Iterable

from django.core.management.base import BaseCommand


@dataclass
class Finding:
    level: str  # "OK", "WARN", "ERROR"
    title: str
    detail: str = ""
    remediation: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def ok(self, title: str, detail: str = "") -> None:
        self.findings.append(Finding("OK", title, detail))

    def warn(self, title: str, detail: str, remediation: str = "") -> None:
        self.findings.append(Finding("WARN", title, detail, remediation))

    def error(self, title: str, detail: str, remediation: str = "") -> None:
        self.findings.append(Finding("ERROR", title, detail, remediation))

    @property
    def exit_code(self) -> int:
        if any(f.level == "ERROR" for f in self.findings):
            return 2
        if any(f.level == "WARN" for f in self.findings):
            return 1
        return 0


class Command(BaseCommand):
    help = (
        "Audit Celery / RabbitMQ broker configuration for common "
        "publisher-reliability misconfigurations."
    )

    def handle(self, *args, **options) -> None:
        report = Report()
        self._check_heartbeat(report)
        self._check_socket_settings(report)
        self._check_confirm_publish(report)
        self._check_worker_recycle(report)
        self._check_queue_types(report)
        self._render(report)
        sys.exit(report.exit_code)

    def _check_heartbeat(self, report: Report) -> None:
        from django.conf import settings

        top_level = getattr(settings, "CELERY_BROKER_HEARTBEAT", None)
        transport_options = (
            getattr(settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {}) or {}
        )
        in_transport = transport_options.get("heartbeat")

        if in_transport:
            report.ok(
                "heartbeat",
                f"transport_options.heartbeat = {in_transport}s",
            )
            return

        if top_level:
            report.warn(
                "heartbeat",
                f"CELERY_BROKER_HEARTBEAT = {top_level} is set at top level "
                "but Celery silently drops it on the publisher path.",
                remediation=(
                    "Move the value into CELERY_BROKER_TRANSPORT_OPTIONS"
                    '["heartbeat"] so it actually reaches kombu.'
                ),
            )
            return

        report.warn(
            "heartbeat",
            "No heartbeat configured. Kombu will negotiate the broker's "
            "default (typically 60s), so dead-connection detection takes "
            "~120s — usually longer than gunicorn's worker timeout.",
            remediation=('Add "heartbeat": 30 to CELERY_BROKER_TRANSPORT_OPTIONS.'),
        )

    def _check_socket_settings(self, report: Report) -> None:
        from django.conf import settings

        transport_options = (
            getattr(settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {}) or {}
        )
        socket_settings = transport_options.get("socket_settings")

        if socket_settings is None:
            report.warn(
                "socket_settings",
                "No socket_settings override; py-amqp defaults give a TCP "
                "keepalive window of ~150s — usually longer than gunicorn's "
                "worker timeout.",
                remediation=(
                    "Add socket_settings to CELERY_BROKER_TRANSPORT_OPTIONS "
                    "with integer keys from the socket module "
                    "(socket.TCP_KEEPIDLE, socket.TCP_KEEPINTVL, "
                    "socket.TCP_KEEPCNT). Targets of 10/5/3 give ~25s detection."
                ),
            )
            return

        if not isinstance(socket_settings, dict):
            report.error(
                "socket_settings",
                f"socket_settings is {type(socket_settings).__name__}, expected dict.",
            )
            return

        string_keys = [k for k in socket_settings if isinstance(k, str)]
        if string_keys:
            report.error(
                "socket_settings",
                f"keys must be integer constants from the socket module, "
                f"but found string key(s): {string_keys!r}. "
                "py-amqp passes them directly to setsockopt(SOL_TCP, ...) "
                "and will raise TypeError on the first connection.",
                remediation=(
                    "Replace string keys with their socket.* integer "
                    "constants — e.g. socket.TCP_KEEPIDLE not 'TCP_KEEPIDLE'."
                ),
            )
            return

        if not socket_settings:
            report.warn(
                "socket_settings",
                "socket_settings dict is empty — no keepalive overrides.",
            )
            return

        report.ok(
            "socket_settings",
            f"{len(socket_settings)} integer-keyed entries: "
            f"{sorted(socket_settings.items())}",
        )

    def _check_confirm_publish(self, report: Report) -> None:
        from django.conf import settings

        transport_options = (
            getattr(settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {}) or {}
        )
        confirm_publish = transport_options.get("confirm_publish")

        if confirm_publish is True:
            report.ok("confirm_publish", "transport_options.confirm_publish = True")
        else:
            report.warn(
                "confirm_publish",
                f"confirm_publish is {confirm_publish!r}. Without publisher "
                "confirms, RabbitMQ may silently drop messages when stressed "
                "(memory high-water-mark, disk full).",
                remediation=(
                    'Set "confirm_publish": True in '
                    "CELERY_BROKER_TRANSPORT_OPTIONS for durable workloads."
                ),
            )

    def _check_worker_recycle(self, report: Report) -> None:
        from django.conf import settings

        value = getattr(settings, "CELERY_WORKER_MAX_TASKS_PER_CHILD", None)
        if value is None or value == 0:
            report.ok(
                "max_tasks_per_child",
                "CELERY_WORKER_MAX_TASKS_PER_CHILD = 0 (no recycle).",
            )
        elif value < 500:
            report.warn(
                "max_tasks_per_child",
                f"CELERY_WORKER_MAX_TASKS_PER_CHILD = {value}. Each recycle "
                "forks a fresh worker that re-handshakes broker connections; "
                "low values multiply that churn.",
                remediation=(
                    "Bump to 1000-2000 unless you have a measured memory "
                    "leak that forces aggressive recycling."
                ),
            )
        else:
            report.ok(
                "max_tasks_per_child",
                f"CELERY_WORKER_MAX_TASKS_PER_CHILD = {value}",
            )

    def _check_queue_types(self, report: Report) -> None:
        from django.conf import settings

        queues = getattr(settings, "CELERY_TASK_QUEUES", None) or ()
        if not queues:
            report.warn(
                "task_queues",
                "CELERY_TASK_QUEUES is empty; queues may be auto-declared "
                "with default settings.",
            )
            return

        rows: list[str] = []
        non_quorum: list[str] = []
        for q in queues:
            name = getattr(q, "name", str(q))
            args = getattr(q, "queue_arguments", None) or {}
            qtype = args.get("x-queue-type", "classic")
            rows.append(f"  {name} → {qtype}")
            if qtype != "quorum":
                non_quorum.append(name)

        detail = "\n".join(rows)
        if non_quorum:
            report.warn(
                "queue_types",
                f"non-quorum queues present (classic mirrored is deprecated "
                f"in RabbitMQ 4.0+): {non_quorum}.\n{detail}",
                remediation=(
                    "Add queue_arguments={'x-queue-type': 'quorum'} to "
                    "each Queue() definition; migrate existing queues with "
                    "the migrate_rabbitmq_queues management command."
                ),
            )
        else:
            report.ok("queue_types", detail)

    def _render(self, report: Report) -> None:
        out = self.stdout
        level_styles = {
            "OK": self.style.SUCCESS,
            "WARN": self.style.WARNING,
            "ERROR": self.style.ERROR,
        }

        for f in report.findings:
            style = level_styles.get(f.level, self.style.NOTICE)
            out.write(style(f"[{f.level}] {f.title}"))
            for line in f.detail.splitlines():
                out.write(f"    {line}")
            if f.remediation:
                out.write(self.style.NOTICE("    fix: ") + f.remediation)
            out.write("")

        # Summary line
        counts = {
            lvl: sum(1 for x in report.findings if x.level == lvl)
            for lvl in ("OK", "WARN", "ERROR")
        }
        summary = (
            f"summary: OK={counts['OK']} WARN={counts['WARN']} ERROR={counts['ERROR']}"
        )
        if report.exit_code == 0:
            out.write(self.style.SUCCESS(summary))
        elif report.exit_code == 1:
            out.write(self.style.WARNING(summary))
        else:
            out.write(self.style.ERROR(summary))


def get_findings_for_settings(settings_obj) -> Iterable[Finding]:
    """Public helper for programmatic use (e.g. tests)."""
    # This function exists so tests can call the audit logic without
    # going through Django's command runner.
    from django.test.utils import override_settings

    cmd = Command()
    report = Report()
    with override_settings():
        cmd._check_heartbeat(report)
        cmd._check_socket_settings(report)
        cmd._check_confirm_publish(report)
        cmd._check_worker_recycle(report)
        cmd._check_queue_types(report)
    return report.findings
