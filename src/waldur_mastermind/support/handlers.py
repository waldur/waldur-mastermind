from constance import config
from django.conf import settings
from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.support.models import Issue

from . import backend, models, tasks


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

    # Skip notifications for forwarded comments (they are system-generated)
    if comment.is_forwarded:
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


def send_issue_created_notification(
    sender, instance: models.Issue, created=False, **kwargs
):
    """Tell helpdesk personnel that a new support request has arrived.

    Only for the built-in service desk. Atlassian, Zammad and SMAX notify their
    own agents, so announcing the ticket again from Waldur would double up.
    """
    if created:
        # The basic backend creates an issue in two saves — first without a
        # backend id, then with one — so the ticket does not properly exist yet.
        # Wait for the key, the same way log_issue_save decides an issue was
        # really created.
        return

    if not instance.key or not instance.tracker.has_changed("key"):
        return

    if config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE != backend.SupportBackendType.BASIC:
        return

    if instance.provider_helpdesk_id:
        # A ticket routed to a provider is a child issue that also passes
        # through BasicBackend.create_issue when that helpdesk is of type
        # basic. It belongs to the provider, who gets notify_provider_new_ticket
        # instead — the operator's staff should not be told twice.
        return

    issue_id = instance.id
    transaction.on_commit(lambda: tasks.notify_staff_new_issue.delay(issue_id))


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


def forward_comment_to_children(
    sender, instance: models.Comment, created=False, **kwargs
):
    """Forward new public comments from parent issues to child issues."""
    if not created:
        return

    # Skip forwarded comments to prevent infinite loops
    if instance.is_forwarded:
        return

    if not instance.is_public:
        return

    parent_issue = instance.issue
    # Only forward if the issue has children (it's a parent)
    if not parent_issue.child_issues.exists():
        return

    comment_id = instance.id
    transaction.on_commit(lambda: tasks.forward_comment_to_child.delay(comment_id))


def propagate_comment_to_parent(
    sender, instance: models.Comment, created=False, **kwargs
):
    """Propagate new public comments from child issues back to parent issues."""
    if not created:
        return

    # Skip forwarded comments to prevent infinite loops
    if instance.is_forwarded:
        return

    if not instance.is_public:
        return

    child_issue = instance.issue
    if not child_issue.parent_issue_id:
        return

    comment_id = instance.id
    transaction.on_commit(lambda: tasks.propagate_comment_to_parent.delay(comment_id))


def dispatch_routing_on_issue_create(
    sender, instance: models.Issue, created=False, **kwargs
):
    """Dispatch routing task when an issue is created or a resource is attached.

    Routing is triggered in two scenarios:
    1. Issue creation — the basic backend creates the issue in two saves
       (first without backend_id, second with it), so we trigger when
       backend_id is first populated.
    2. Resource attachment — operator connects a resource to an existing
       issue that had no resource, enabling provider routing.
    """
    if not config.WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED:
        return

    # Skip child issues (already routed)
    if instance.parent_issue_id is not None:
        return

    # Skip if already routed
    if instance.child_issues.exists():
        return

    # Skip if no backend_id yet (issue not fully created)
    if not instance.backend_id:
        return

    should_route = False

    # Scenario 1: backend_id first populated (issue creation)
    if created or (
        instance.tracker.has_changed("backend_id")
        and not instance.tracker.previous("backend_id")
    ):
        # Route if a resource or an offering is already attached; either is
        # enough to resolve the provider (see resolve_routing_offering).
        if instance.resource_object_id or instance.offering_id:
            should_route = True

    # Scenario 2: a resource or an offering is first attached to an existing issue
    if not created and (
        (
            instance.resource_object_id
            and instance.tracker.has_changed("resource_object_id")
            and not instance.tracker.previous("resource_object_id")
        )
        or (
            instance.offering_id
            and instance.tracker.has_changed("offering_id")
            and not instance.tracker.previous("offering_id")
        )
    ):
        should_route = True

    if should_route:
        issue_id = instance.id
        transaction.on_commit(lambda: tasks.route_issue_to_provider.delay(issue_id))


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
