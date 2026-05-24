from django.contrib.contenttypes.models import ContentType
from rest_framework import exceptions, permissions

from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import get_users, has_permission, permission_factory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import (
    RequestedOfferingStates,
    ResponsibleRoles,
    WorkflowStepInstanceStatuses,
)

user_can_accept_requested_offering = permission_factory(
    PermissionEnum.ACCEPT_REQUESTED_OFFERING,
    ["offering.customer"],
)


class CanUpdateCallPermission(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        call = obj.call
        return has_permission(
            request,
            PermissionEnum.UPDATE_CALL,
            call,
        )


# Map proposal.ResponsibleRoles → permission system role on the call.
# Roles handled by special-case branches (applicant, offering_manager,
# panel_member) are intentionally absent from this dict — see
# _user_can_act_on_active_step. panel_member is denied via the explicit
# branch below; mapping a future panel system role here will enable it.
RESPONSIBLE_ROLE_TO_CALL_ROLE = {
    ResponsibleRoles.CALL_MANAGER: RoleEnum.CALL_MANAGER,
    ResponsibleRoles.REVIEWER: RoleEnum.CALL_REVIEWER,
}


def _user_holds_offering_manager_for_call(user, call):
    """True iff the user holds OFFERING.MANAGER on any accepted offering of the call.

    Single ``UserRole`` query against the set of accepted offering ids — one
    round-trip regardless of how many offerings the call has. Mirrors
    ``get_users``' filters (``is_active=True`` + role name).
    """
    offering_ids = proposal_models.RequestedOffering.objects.filter(
        call=call, state=RequestedOfferingStates.ACCEPTED
    ).values_list("offering_id", flat=True)
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        user=user,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=offering_ct,
        object_id__in=offering_ids,
    ).exists()


def _user_can_act_on_active_step(user, proposal):
    """True iff the user has the role required by the proposal's active workflow step.

    Staff bypass this check. When no active step exists or the active step has
    no responsible_role configured, access is allowed and the view's own
    state/concurrency guards take over.
    """
    if user.is_staff:
        return True

    active = proposal.workflow_step_instances.filter(
        status=WorkflowStepInstanceStatuses.ACTIVE
    ).first()
    if not active:
        # No active step — let the view's own guards return the right error.
        return True

    call = proposal.round.call
    call_step = proposal_models.CallWorkflowStep.objects.filter(
        call=call, step=active.step
    ).first()
    if not call_step or not call_step.responsible_role:
        return True

    role = call_step.responsible_role
    if role == ResponsibleRoles.APPLICANT:
        return proposal.created_by_id == user.id

    if role == ResponsibleRoles.OFFERING_MANAGER:
        return _user_holds_offering_manager_for_call(user, call)

    if role == ResponsibleRoles.PANEL_MEMBER:
        # No system role exists yet for panel members — fail closed so the
        # call manager has to either reassign the step or implement the role.
        return False

    role_name = RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if not role_name:
        # Unrecognised responsible_role — fail closed rather than allow.
        return False

    return get_users(call, role_name=role_name).filter(pk=user.pk).exists()


def can_act_on_active_workflow_step(request, view, obj=None):
    """ActionsPermission check for complete/reject workflow step actions."""
    if obj is None:
        return
    if not _user_can_act_on_active_step(request.user, obj):
        raise exceptions.PermissionDenied(
            "You don't hold the role required to act on the active workflow step."
        )


# Mark check as needing the object so ActionsPermission fetches it.
can_act_on_active_workflow_step.sources = ["*"]


def can_advance_workflow_step(request, view, obj=None):
    """Only call managers (or staff) may manually advance a workflow."""
    if obj is None:
        return
    user = request.user
    if user.is_staff:
        return
    call = obj.round.call
    if not get_users(call, role_name=RoleEnum.CALL_MANAGER).filter(pk=user.pk).exists():
        raise exceptions.PermissionDenied(
            "Only the call manager can advance this workflow step."
        )


can_advance_workflow_step.sources = ["*"]
