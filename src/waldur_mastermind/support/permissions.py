"""Who may act on a support issue, a comment or a provider helpdesk's records.

Each check enforces one rule and describes it in the API schema, so what a
route enforces and what the schema says come from one place.
"""

from rest_framework import exceptions

from waldur_core.core import access
from waldur_core.core.permissions import has_staff_access, has_support_access
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.permissions import PROJECT_ADMIN_ROLES
from waldur_mastermind.support import models


def comments_on_issue(via=None) -> dict:
    """Who ``can_comment_on_issue`` lets comment, for the API schema.

    ``via`` names the request field the issue comes from, where it is not the
    object acted on.
    """
    return access.any_of(
        access.staff_or_support(),
        access.owned("The issue's caller."),
        access.role(PROJECT_ADMIN_ROLES, via=via),
        access.custom(
            "An active agent of the provider helpdesk the issue is routed to."
        ),
    )


@access.describes(comments_on_issue(), needs_object=True)
def can_comment_on_issue(request, view, obj: models.Issue | None = None):
    user = request.user
    if has_support_access(request) or not obj:
        return
    issue = obj
    # The caller may reply on their own ticket. Scope is already settled by
    # the time this runs: reaching here means the issue survived
    # `IssueCallerOrRoleFilterBackend`, which on a ticket raised against a
    # project or an organization admits only the roles held there. So this
    # covers the caller who still has access but holds none of the roles
    # below -- a plain project member on their own thread, refused with a
    # 403 while the UI went on offering them the button.
    if issue.caller == user:
        return
    if issue.customer and issue.customer.has_user(user, CustomerRole.OWNER):
        return
    if issue.project and (
        issue.project.has_user(user, ProjectRole.ADMIN)
        or issue.project.has_user(user, ProjectRole.MANAGER)
    ):
        return
    # Provider support users can comment on tickets routed to their helpdesk.
    if (
        issue.provider_helpdesk
        and issue.provider_helpdesk.support_users.filter(
            user=user, is_active=True
        ).exists()
    ):
        return
    raise exceptions.PermissionDenied()


@access.describes(
    access.any_of(
        access.staff(),
        access.owned(
            "The attachment's uploader, while they may still comment on "
            "its issue, as attaching requires."
        ),
    ),
    needs_object=True,
)
def can_delete_attachment(request, view, obj: models.Attachment | None = None):
    # `AttachmentSerializer.can_delete` reuses one verdict for every attachment
    # with the same uploader and issue, so read nothing else of the attachment
    # here without changing its key.
    if obj is None or has_staff_access(request):
        return
    if obj.author is None or obj.author.user_id != request.user.id:
        raise exceptions.PermissionDenied()
    can_comment_on_issue(request, view, obj.issue)
