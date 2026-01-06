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
