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

    @staticmethod
    def get_scopes(event_context):
        issue = event_context['issue']
        result = set()
        if issue.resource:
            project = issue.resource.service_project_link.project
            result.add(issue.resource)
            result.add(project)
            result.add(project.customer)
        if issue.project:
            result.add(issue.project)
            result.add(issue.customer)
        if issue.customer:
            result.add(issue.customer)
        return result


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

    @staticmethod
    def get_scopes(event_context):
        offering = event_context['offering']
        return {offering.project, offering.project.customer}


event_logger.register('waldur_issue', IssueEventLogger)
event_logger.register('waldur_offering', OfferingEventLogger)
