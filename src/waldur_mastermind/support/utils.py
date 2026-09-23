from email.utils import parseaddr

from django.conf import settings

from waldur_core.core.utils import format_homeport_link


def get_issue_thread_id(issue_uuid) -> str:
    """Stable message id standing for the whole mail thread of one ticket.

    Takes the UUID rather than the issue, because a ticket can still have mail
    to send once its row is gone: a child issue withdrawn from a provider is
    deleted before the withdrawal notice goes out.
    """
    sender = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    _, at, domain = sender.rpartition("@")
    if not at or not domain:
        domain = "localhost"
    return f"<support-issue-{issue_uuid.hex}@{domain}>"


def get_issue_thread_headers(issue_uuid) -> dict[str, str]:
    """Threading headers for a notification about the issue with that UUID.

    No message claims the thread id as its own ``Message-ID``. Notifications
    fan out as one message per recipient, so a shared id would break RFC 5322
    uniqueness, and a mailbox that receives it twice -- through an alias, a
    group address, or a retried task -- suppresses the second copy. The thread
    therefore has no root message, which costs nothing: a ``References`` chain
    pointing at a message the client has never seen is the ordinary case for
    mail clients, and every copy keeps a unique ``Message-ID`` of its own.
    """
    thread_id = get_issue_thread_id(issue_uuid)
    return {"In-Reply-To": thread_id, "References": thread_id}


def format_issue_subject(subject: str, issue) -> str:
    """Prefix a rendered subject with the ticket key.

    Clients that thread by subject need every mail about one ticket to share a
    prefix. Most subject templates carry ``[{{ issue.key }}]`` already; this
    covers the notifications whose subject may come from a
    ``TemplateStatusNotification`` row instead of a template file.
    """
    prefix = f"[{issue.key or issue.uuid.hex[:8]}]"
    if subject.startswith(prefix):
        return subject
    return f"{prefix} {subject}"


def get_feedback_link(token, evaluation=""):
    return format_homeport_link(
        "support/feedback/?token={token}&evaluation={evaluation}",
        token=token,
        evaluation=evaluation,
    )


def get_default_request_type():
    """Get the default (first active) request type name."""
    from waldur_mastermind.support import models

    first_type = models.RequestType.objects.filter(is_active=True).first()
    return first_type.name if first_type else ""


# Kept for backward compatibility - delegates to new function
def get_atlassian_issue_type():
    return get_default_request_type()


def get_helpdesk_stats():
    """Compute comprehensive helpdesk statistics."""
    from datetime import date

    from django.db.models import Avg, Count, ExpressionWrapper, F, fields

    from waldur_mastermind.support import models

    today = date.today()
    # Same definition /api/support-statistics/ and the is_open filter use.
    # Keying off resolution_date alone counted every terminal-status issue that
    # predates that field being stamped — and every Jira-synced issue, since the
    # Atlassian backend never sets it — as open here while the support
    # statistics called it closed.
    open_issues = models.Issue.objects.open()

    stats = {
        "total_open": open_issues.count(),
        "total_closed_this_month": models.Issue.objects.filter(
            resolution_date__month=today.month,
            resolution_date__year=today.year,
        ).count(),
        "total_routed": models.Issue.objects.filter(
            child_issues__isnull=False,
        )
        .distinct()
        .count(),
        "total_escalated": models.Issue.objects.filter(is_escalated=True).count(),
        "sla_breach_count": models.Issue.objects.filter(sla_breached=True).count(),
    }

    # Average first response time
    responded = models.Issue.objects.filter(
        first_response_at__isnull=False,
    ).annotate(
        response_time=ExpressionWrapper(
            F("first_response_at") - F("created"),
            output_field=fields.DurationField(),
        )
    )
    avg_response = responded.aggregate(avg=Avg("response_time"))["avg"]
    stats["avg_first_response_hours"] = (
        avg_response.total_seconds() / 3600 if avg_response else None
    )

    # Average resolution time
    resolved = models.Issue.objects.filter(
        resolution_date__isnull=False,
    ).annotate(
        resolve_time=ExpressionWrapper(
            F("resolution_date") - F("created"),
            output_field=fields.DurationField(),
        )
    )
    avg_resolve = resolved.aggregate(avg=Avg("resolve_time"))["avg"]
    stats["avg_resolution_hours"] = (
        avg_resolve.total_seconds() / 3600 if avg_resolve else None
    )

    # By status breakdown
    by_status = dict(
        open_issues.values_list("status")
        .annotate(count=Count("id"))
        .values_list("status", "count")
    )
    stats["by_status"] = by_status

    # By priority breakdown
    by_priority = dict(
        open_issues.values_list("priority")
        .annotate(count=Count("id"))
        .values_list("priority", "count")
    )
    stats["by_priority"] = by_priority

    return stats


def build_provider_context(issue, resource=None):
    """Build enriched context dict for provider issue description."""
    context = {
        "summary": issue.summary,
        "description": issue.description,
        "type": issue.type,
        "priority": issue.priority,
        "key": issue.key,
    }

    if issue.caller:
        context["caller_name"] = issue.caller.full_name
        context["caller_email"] = issue.caller.email

    if issue.customer:
        context["customer_name"] = issue.customer.name

    if issue.project:
        context["project_name"] = issue.project.name

    if resource:
        context["resource_name"] = getattr(resource, "name", str(resource))

    return context
