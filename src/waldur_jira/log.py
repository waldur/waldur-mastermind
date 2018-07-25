from waldur_core.logging.loggers import EventLogger, event_logger

from .models import Issue, Comment


class IssueEventLogger(EventLogger):
    issue = Issue

    class Meta:
        event_types = ('issue_deletion_succeeded',
                       'issue_update_succeeded',
                       'issue_creation_succeeded')
        event_groups = {
            'jira': event_types
        }


class CommentEventLogger(EventLogger):
    comment = Comment

    class Meta:
        event_types = ('comment_deletion_succeeded',
                       'comment_update_succeeded',
                       'comment_creation_succeeded')
        event_groups = {
            'jira': event_types
        }


event_logger.register('jira_issue', IssueEventLogger)
event_logger.register('jira_comment', CommentEventLogger)
