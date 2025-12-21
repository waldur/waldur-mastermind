from django.db import models


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


class SupportIssueType(models.TextChoices):
    INFORMATIONAL = "INFORMATIONAL", "Informational"
    SERVICE_REQUEST = "SERVICE_REQUEST", "Service request"
    CHANGE_REQUEST = "CHANGE_REQUEST", "Change request"
    INCIDENT = "INCIDENT", "Incident"


class SupportIssueStatusType(models.IntegerChoices):
    RESOLVED = 0, "Resolved"
    CANCELED = 1, "Canceled"
