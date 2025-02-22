class SupportWebhookEvent:
    ISSUE_UPDATE = 2
    ISSUE_DELETE = 4
    COMMENT_CREATE = 5
    COMMENT_UPDATE = 6
    COMMENT_DELETE = 7

    ISSUE_ACTIONS = (ISSUE_UPDATE, ISSUE_DELETE)
    COMMENT_ACTIONS = (COMMENT_CREATE, COMMENT_UPDATE, COMMENT_DELETE)

    CHOICES = (
        ("jira:issue_updated", ISSUE_UPDATE),
        ("jira:issue_deleted", ISSUE_DELETE),
        ("comment_created", COMMENT_CREATE),
        ("comment_updated", COMMENT_UPDATE),
        ("comment_deleted", COMMENT_DELETE),
    )
