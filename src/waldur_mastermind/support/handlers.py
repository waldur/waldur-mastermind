from django.conf import settings
from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.support.models import Issue

from . import models, tasks


def get_issue_scopes(issue: Issue) -> set:
    result = set()
    project_id = None

    resource = issue.safe_resource
    if resource is not None:
        try:
            project_id = resource.project_id
            result.add(resource)
        except (AttributeError, ValueError):
            # Resource may have been deleted, causing ContentType issues
            pass

    if not project_id:
        project_id = issue.project_id

    project = None
    try:
        project = Project.all_objects.get(id=project_id)
    except Project.DoesNotExist:
        pass
    if project:
        result.add(project)
        try:
            if project.customer:
                result.add(project.customer)
        except Customer.DoesNotExist:
            # Customer may have been deleted in a cascade
            pass

    # Try to access customer, but handle case where it's already deleted
    try:
        if issue.customer:
            result.add(issue.customer)
    except Customer.DoesNotExist:
        # Customer may have been deleted in a cascade
        pass

    return result


def log_issue_save(sender, instance: models.Issue, created=False, **kwargs):
    if created:
        return

    if not instance.key:
        # If issue does not have key, it is not actually created on backend.
        # Therefore it is okay to skip logging in this case.
        return

    # If issue got a key, it means that it has been actually created on backend.
    if instance.tracker.has_changed("key"):
        event_logger.emit(
            "Issue {issue_key} has been created.",
            event_type=EventType.ISSUE_CREATION_SUCCEEDED,
            event_context={
                "issue": instance,
            },
            scopes=get_issue_scopes(instance),
        )
    else:
        updated_fields = instance.tracker.changed()
        updated_fields.pop("modified", None)  # waldur-specific field
        if len(updated_fields.keys()) > 0:
            event_logger.emit(
                "Issue {issue_key} has been updated. Changed fields: %s."
                % ", ".join(updated_fields.keys()),
                event_type=EventType.ISSUE_UPDATE_SUCCEEDED,
                event_context={
                    "issue": instance,
                },
                scopes=get_issue_scopes(instance),
            )


def log_issue_delete(sender, instance: models.Issue, **kwargs):
    if not instance.key:
        # If issue does not have key, it is not actually created on backend.
        # Therefore it is okay to skip logging in this case.
        return

    event_logger.emit(
        "Issue {issue_key} has been deleted.",
        event_type=EventType.ISSUE_DELETION_SUCCEEDED,
        event_context={
            "issue": instance,
        },
        scopes=get_issue_scopes(instance),
    )


def log_attachment_save(sender, instance: models.Attachment, created=False, **kwargs):
    if created:
        event_logger.emit(
            "Attachment for issue {issue_key} has been created.",
            event_type=EventType.ATTACHMENT_CREATED,
            event_context={
                "attachment": instance,
            },
            scopes=get_issue_scopes(instance.issue),
        )
    else:
        event_logger.emit(
            "Attachment for issue {issue_key} has been updated.",
            event_type=EventType.ATTACHMENT_UPDATED,
            event_context={
                "attachment": instance,
            },
            scopes=get_issue_scopes(instance.issue),
        )


def log_attachment_delete(sender, instance: models.Attachment, **kwargs):
    event_logger.emit(
        "Attachment for issue {issue_key} has been deleted.",
        event_type=EventType.ATTACHMENT_DELETED,
        event_context={
            "attachment": instance,
        },
        scopes=get_issue_scopes(instance.issue),
    )


def send_comment_added_notification(
    sender, instance: models.Comment, created=False, **kwargs
):
    comment = instance

    # Skip notifications for private comments
    if not comment.is_public:
        return

    # Skip notifications about comments added to an issue by caller himself
    if comment.author.user == comment.issue.caller:
        return

    serialized_comment = core_utils.serialize_instance(comment)
    if created:
        transaction.on_commit(
            lambda: tasks.send_comment_added_notification.delay(serialized_comment)
        )
    else:
        old_description = comment.tracker.previous("description")
        if old_description != comment.description:
            transaction.on_commit(
                lambda: tasks.send_comment_updated_notification.delay(
                    serialized_comment, old_description
                )
            )


def send_issue_updated_notification(
    sender, instance: models.Issue, created=False, **kwargs
):
    issue = instance

    # Skip notification if issue just have been created in Waldur
    if created:
        return

    # Skip notification if issue is not created on backend yet.
    if not instance.backend_id:
        return

    # Skip notifications if assignee or modification date changed
    tracked_fields = ("summary", "description", "status", "priority")
    changed = dict(
        (field, instance.tracker.previous(field))
        for field in instance.tracker.fields
        if instance.tracker.has_changed(field) and field in tracked_fields
    )

    if not changed:
        return

    # Skip notification if issue status is ignored.
    if (
        "status" in changed
        and models.IgnoredIssueStatus.objects.filter(name=issue.status).exists()
    ):
        return

    serialized_issue = core_utils.serialize_instance(instance)

    transaction.on_commit(
        lambda: tasks.send_issue_updated_notification.delay(serialized_issue, changed)
    )


def create_feedback_if_issue_has_been_resolved(
    sender, instance: models.Issue, created=False, **kwargs
):
    """Create feedback request when support issue transitions to resolved state."""
    if not settings.ISSUE_FEEDBACK_ENABLE:
        return

    issue = instance

    if created:
        return

    if (
        not issue.tracker.has_changed("status")
        or not issue.resolved
        or not issue.feedback_request
        or models.IssueStatus.check_success_status(issue.tracker.previous("status"))
    ):
        return

    serialized_issue = core_utils.serialize_instance(issue)

    transaction.on_commit(
        lambda: tasks.send_issue_feedback_notification.delay(serialized_issue)
    )
