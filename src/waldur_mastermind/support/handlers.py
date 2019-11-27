from django.db import transaction

from waldur_core.core import utils as core_utils

from .log import event_logger
from . import tasks, models


def log_issue_save(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.key:
        # If issue does not have key, it is not actually created on backend.
        # Therefore it is okay to skip logging in this case.
        return

    # If issue got a key, it means that it has been actually created on backend.
    if instance.tracker.has_changed('key'):
        event_logger.waldur_issue.info(
            'Issue {issue_key} has been created.',
            event_type='issue_creation_succeeded',
            event_context={
                'issue': instance,
            })
    else:
        updated_fields = instance.tracker.changed()
        updated_fields.pop('modified')  # waldur-specific field
        event_logger.waldur_issue.info(
            'Issue {issue_key} has been updated. Changed fields: %s.' % ", ".join(updated_fields.keys()),
            event_type='issue_update_succeeded',
            event_context={
                'issue': instance,
            })


def log_issue_delete(sender, instance, **kwargs):
    if not instance.key:
        # If issue does not have key, it is not actually created on backend.
        # Therefore it is okay to skip logging in this case.
        return

    event_logger.waldur_issue.info(
        'Issue {issue_key} has been deleted.',
        event_type='issue_deletion_succeeded',
        event_context={
            'issue': instance,
        })


def log_attachment_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.waldur_attachment.info(
            'Attachment for issue {issue_key} has been created.',
            event_type='attachment_created',
            event_context={
                'attachment': instance,
            })
    else:
        event_logger.waldur_attachment.info(
            'Attachment for issue {issue_key} has been updated.',
            event_type='attachment_updated',
            event_context={
                'attachment': instance,
            })


def log_attachment_delete(sender, instance, **kwargs):
    event_logger.waldur_attachment.info(
        'Attachment for issue {issue_key} has been deleted.',
        event_type='attachment_deleted',
        event_context={
            'attachment': instance,
        })


def log_offering_created(sender, instance, created=False, **kwargs):
    if created:
        event_logger.waldur_offering.info(
            'Offering {offering_name} has been created.',
            event_type='offering_created',
            event_context={
                'offering': instance,
            })


def log_offering_deleted(sender, instance, **kwargs):
    event_logger.waldur_offering.info(
        'Offering {offering_name} has been deleted.',
        event_type='offering_deleted',
        event_context={
            'offering': instance,
        })


def log_offering_state_changed(sender, instance, **kwargs):
    state = instance.state
    if state != instance.tracker.previous('state'):
        event_logger.waldur_offering.info(
            'Offering state has changed to {offering_state}',
            event_type='offering_state_changed',
            event_context={
                'offering': instance,
            }
        )


def send_comment_added_notification(sender, instance, created=False, **kwargs):
    comment = instance

    # Skip notifications for private comments
    if not comment.is_public:
        return

    # Skip notifications about comments added to an issue by caller himself
    if comment.author.user == comment.issue.caller:
        return

    serialized_comment = core_utils.serialize_instance(comment)
    if created:
        transaction.on_commit(lambda:
                              tasks.send_comment_added_notification.delay(serialized_comment))
    else:
        old_description = comment.tracker.previous('description')
        transaction.on_commit(lambda:
                              tasks.send_comment_updated_notification.delay(serialized_comment, old_description))


def send_issue_updated_notification(sender, instance, created=False, **kwargs):
    issue = instance

    # Skip notification if issue just have been created in Waldur
    if created:
        return

    # Skip notification if issue is not created on backend yet.
    if not instance.backend_id:
        return

    # Skip notification if issue just has been created on backend.
    if 'backend_id' in instance.tracker.changed():
        return

    # Skip notifications if assignee or modification date changed
    tracked_fields = ('summary', 'description', 'status', 'priority')
    changed = dict(
        (field, instance.tracker.previous(field))
        for field in instance.tracker.fields
        if instance.tracker.has_changed(field) and field in tracked_fields
    )

    if not changed:
        return

    # Skip notification if issue status is ignored.
    if 'status' in changed and \
            models.IgnoredIssueStatus.objects.filter(name=issue.status).exists():
        return

    serialized_issue = core_utils.serialize_instance(instance)

    transaction.on_commit(lambda:
                          tasks.send_issue_updated_notification.delay(serialized_issue, changed))
