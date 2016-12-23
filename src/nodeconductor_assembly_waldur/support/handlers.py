from .log import event_logger


def log_issue_save(sender, instance, created=False, **kwargs):
    if created:
        event_logger.waldur_issue.info(
            'Issue {issue_key} has been created.',
            event_type='issue_creation_succeeded',
            event_context={
                'issue': instance,
            })
    else:
        event_logger.waldur_issue.info(
            'Issue {issue_key} has been updated.',
            event_type='issue_update_succeeded',
            event_context={
                'issue': instance,
            })


def log_issue_delete(sender, instance, **kwargs):
    event_logger.waldur_issue.info(
        'Issue {issue_key} has been deleted.',
        event_type='issue_deletion_succeeded',
        event_context={
            'issue': instance,
        })
