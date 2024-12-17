from waldur_core.logging.loggers import EventLogger, event_logger

from . import models


def get_issue_scopes(issue):
    from waldur_core.structure.models import Project

    result = set()
    if issue.resource:
        project_id = issue.resource.project_id
        result.add(issue.resource)
    else:
        project_id = issue.project_id
    project = None
    try:
        project = Project.all_objects.get(id=project_id)
    except Project.DoesNotExist:
        pass
    if project:
        result.add(project)
        result.add(project.customer)
    if issue.customer:
        result.add(issue.customer)
    return result


class IssueEventLogger(EventLogger):
    issue = models.Issue

    class Meta:
        event_types = (
            "issue_deletion_succeeded",
            "issue_update_succeeded",
            "issue_creation_succeeded",
        )
        event_groups = {
            "support": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        issue = event_context["issue"]
        return get_issue_scopes(issue)


class AttachmentEventLogger(EventLogger):
    attachment = models.Attachment

    class Meta:
        event_types = (
            "attachment_created",
            "attachment_updated",
            "attachment_deleted",
        )
        event_groups = {
            "support": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        attachment = event_context["attachment"]
        return get_issue_scopes(attachment.issue)


event_logger.register("waldur_issue", IssueEventLogger)
event_logger.register("waldur_attachment", AttachmentEventLogger)
