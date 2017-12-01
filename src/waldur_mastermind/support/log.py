from waldur_core.logging.loggers import EventLogger, event_logger

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


class OfferingEventLogger(EventLogger):
    offering = models.Offering

    class Meta:
        event_types = (
            'offering_created',
            'offering_deleted',
            'offering_state_changed',
        )
        event_groups = {
            'support': event_types,
        }


event_logger.register('waldur_issue', IssueEventLogger)
event_logger.register('waldur_offering', OfferingEventLogger)
