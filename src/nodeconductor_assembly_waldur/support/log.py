from nodeconductor.logging.loggers import EventLogger, event_logger

from . import models


class IssueEventLogger(EventLogger):
    issue = models.Issue

    class Meta:
        event_types = ('issue_deletion_succeeded',
                       'issue_update_succeeded',
                       'issue_creation_succeeded')
        event_groups = {
            'support': event_types,
        }

event_logger.register('waldur_issue', IssueEventLogger)
