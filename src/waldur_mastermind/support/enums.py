from django.db import models


class IssueStatusTypes:
    RESOLVED = 0
    CANCELED = 1


ISSUE_STATUS_TYPE_CHOICES = (
    (IssueStatusTypes.RESOLVED, "Resolved"),
    (IssueStatusTypes.CANCELED, "Canceled"),
)


class SupportWebhookEvent(models.IntegerChoices):
    ISSUE_UPDATE = 2, "jira:issue_updated"
    ISSUE_DELETE = 4, "jira:issue_deleted"
    COMMENT_CREATE = 5, "comment_created"
    COMMENT_UPDATE = 6, "comment_updated"
    COMMENT_DELETE = 7, "comment_deleted"


COMMENT_ACTIONS = (
    SupportWebhookEvent.COMMENT_CREATE,
    SupportWebhookEvent.COMMENT_UPDATE,
    SupportWebhookEvent.COMMENT_DELETE,
)

JIRA_WEBHOOK_EVENT_MAP = {label: value for value, label in SupportWebhookEvent.choices}
