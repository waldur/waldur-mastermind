"""Who may act on a support issue, a comment or a provider helpdesk's records.

Each check enforces one rule and describes it in the API schema, so what a
route enforces and what the schema says come from one place.
"""

from rest_framework import exceptions

from waldur_core.core import access
from waldur_core.core.permissions import has_staff_access, has_support_access
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.permissions import PROJECT_ADMIN_ROLES
from waldur_mastermind.support import backend, models


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
            "its issue, as attaching requires. Not on a ticket whose provider "
            "helpdesk has been deleted, where only staff may delete."
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
    if backend.is_orphaned(obj.issue):
        # Deleted in Waldur alone, and no desk keeps a copy: staff decide.
        raise exceptions.PermissionDenied()
    can_comment_on_issue(request, view, obj.issue)


def _changes_comment(verb):
    # What backend.comment_change_is_permitted lets through. Change the two
    # together.
    return access.custom(
        f"Staff, or the comment's author where the active support backend "
        f"(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE) lets authors {verb} their own "
        "comments, as the built-in desk, the default, does; Atlassian, Zammad "
        "and SMAX do not. The author may not once the comment's ticket is "
        "routed to or from a provider helpdesk, nor for a comment forwarded "
        "between such tickets."
    )


@access.describes(_changes_comment("edit"), needs_object=True)
def can_edit_comment(request, view, obj: models.Comment | None = None):
    if obj is None:
        return
    # The backend that holds the ticket decides: on a ticket routed to a
    # provider, that is the provider helpdesk's, not the operator's. A ticket
    # whose helpdesk was deleted has none, and its author may not change it.
    issue_backend = backend.find_backend_for_issue(obj.issue)
    if not backend.comment_change_is_permitted(
        request,
        obj,
        bool(issue_backend and issue_backend.comment_author_update_is_supported),
    ):
        raise exceptions.PermissionDenied()


@access.describes(_changes_comment("delete"), needs_object=True)
def can_delete_comment(request, view, obj: models.Comment | None = None):
    if obj is None:
        return
    issue_backend = backend.find_backend_for_issue(obj.issue)
    if not backend.comment_change_is_permitted(
        request,
        obj,
        bool(issue_backend and issue_backend.comment_author_destroy_is_supported),
    ):
        raise exceptions.PermissionDenied()


def _owns_service_provider_of(request, helpdesk: models.ProviderHelpdesk) -> bool:
    return helpdesk.service_provider.customer.has_user(request.user, CustomerRole.OWNER)


@access.describes(
    access.any_of(
        access.staff(),
        access.custom(
            "An owner of the organization of the helpdesk's service provider."
        ),
    ),
    needs_object=True,
)
def owns_helpdesk(request, view, obj: models.ProviderHelpdesk | None = None):
    if has_staff_access(request) or obj is None:
        return
    if not _owns_service_provider_of(request, obj):
        raise exceptions.PermissionDenied()


@access.describes(
    access.any_of(
        access.staff(),
        access.custom(
            "An owner of the organization of the service provider whose "
            "helpdesk it belongs to."
        ),
    ),
    needs_object=True,
)
def owns_helpdesk_of(request, view, obj=None):
    """For a record of a provider helpdesk, such as an agent or a canned response."""
    if has_staff_access(request) or obj is None:
        return
    if not _owns_service_provider_of(request, obj.provider_helpdesk):
        raise exceptions.PermissionDenied()


def active_agent(user, helpdesk) -> models.ProviderSupportUser | None:
    """The user's active agent account in ``helpdesk``, if they have one."""
    return models.ProviderSupportUser.objects.filter(
        user=user, provider_helpdesk=helpdesk, is_active=True
    ).first()


def not_an_agent() -> exceptions.ValidationError:
    # A validation error rather than a refusal, as when the claim action
    # itself made this check.
    return exceptions.ValidationError(
        "You are not a support user in this provider's helpdesk."
    )


@access.describes(
    access.custom("An active agent of the helpdesk the ticket is routed to."),
    needs_object=True,
)
def is_agent_of_ticket_helpdesk(request, view, obj: models.Issue | None = None):
    if obj is not None and not active_agent(request.user, obj.provider_helpdesk):
        raise not_an_agent()
