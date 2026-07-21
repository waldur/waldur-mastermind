from waldur_core.core.utils import format_homeport_link


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
    open_issues = models.Issue.objects.filter(resolution_date__isnull=True)

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
