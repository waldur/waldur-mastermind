from waldur_core.logging.loggers import EventLogger, event_logger

from .models import Issue, Comment


def get_issue_scopes(issue):
    project = issue.project.service_project_link.project
    result = {project, project.customer}
    if issue.resource:
        result.add(issue.resource)
    return result


class IssueEventLogger(EventLogger):
    issue = Issue

    class Meta:
        event_types = ('issue_deletion_succeeded',
                       'issue_update_succeeded',
                       'issue_creation_succeeded')
        event_groups = {
            'jira': event_types
        }

    @staticmethod
    def get_scopes(event_context):
        issue = event_context['issue']
        return get_issue_scopes(issue)


class CommentEventLogger(EventLogger):
    comment = Comment

    class Meta:
        event_types = ('comment_deletion_succeeded',
                       'comment_update_succeeded',
                       'comment_creation_succeeded')
        event_groups = {
            'jira': event_types
        }

    @staticmethod
    def get_scopes(event_context):
        issue = event_context['comment'].issue
        return get_issue_scopes(issue)


event_logger.register('jira_issue', IssueEventLogger)
event_logger.register('jira_comment', CommentEventLogger)
