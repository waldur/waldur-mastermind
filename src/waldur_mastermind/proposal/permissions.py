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
# Roles handled by special-case branches (applicant, offering_manager) are
# intentionally absent from this dict — see _user_can_act_on_active_step.
RESPONSIBLE_ROLE_TO_CALL_ROLE = {
    ResponsibleRoles.CALL_MANAGER: RoleEnum.CALL_MANAGER,
    ResponsibleRoles.REVIEWER: RoleEnum.CALL_REVIEWER,
    ResponsibleRoles.PANEL_MEMBER: RoleEnum.CALL_PANEL_MEMBER,
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
        # award_response is the applicant's to complete; the call manager does
        # not act on the applicant's behalf here.
        return proposal.created_by_id == user.id

    # Call managers drive step progression: they may complete/reject any
    # non-applicant step. The step's review/score gates (_enforce_step_gates)
    # remain the guardrail, so this is not a rubber stamp. The step's own
    # responsible actor (reviewer / panel member / offering manager) may also
    # act on it.
    if get_users(call, role_name=RoleEnum.CALL_MANAGER).filter(pk=user.pk).exists():
        return True

    if role == ResponsibleRoles.OFFERING_MANAGER:
        return _user_holds_offering_manager_for_call(user, call)

    role_name = RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if not role_name:
        # Unrecognised responsible_role — fail closed rather than allow.
        return False

    return get_users(call, role_name=role_name).filter(pk=user.pk).exists()


def user_can_view_internal_notes(user, proposal):
    """True iff the user is part of the call-management team for this proposal.

    Applicants never see internal workflow notes (the entire point of the
    field is to be hidden from them). Staff always see them. Otherwise the
    user must hold any active role on the proposal's call — call manager,
    offering manager, reviewer, panel member, or any future call-level role —
    so that callers don't lose access to historical notes once the workflow
    moves past the step they personally owned.

    Dual-role policy (applicant who also holds a call role on their own
    proposal): the applicant-first deny is intentional segregation of duties.
    A user reading notes about their own proposal is the read-counterparty,
    not the reviewing team — even if they happen to be a call manager on the
    same call. ``_user_can_act_on_active_step`` makes the opposite trade-off
    (lets dual-role users act on call-manager-owned steps) because acting on
    a step is governed by the step's responsible role, not by the proposal's
    subject; viewing notes about a proposal is the reverse. See the test
    ``test_dual_role_applicant_still_denied`` for the pinned behaviour.
    """
    if user.is_staff:
        return True
    if user.id == proposal.created_by_id:
        return False
    call_ct = ContentType.objects.get_for_model(proposal_models.Call)
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        user=user,
        content_type=call_ct,
        object_id=proposal.round.call_id,
    ).exists()


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
