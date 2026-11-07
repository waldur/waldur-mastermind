import uuid

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import exceptions, permissions

from waldur_core.core import access
from waldur_core.core.models import User
from waldur_core.core.permissions import has_staff_access, has_support_access
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import (
    check_pat_support_scope,
    get_scope_ids,
    get_users,
    has_permission,
    has_permission_on_any_source,
    has_user,
    permission_factory,
)
from waldur_core.structure.managers import get_connected_customers_by_permission
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import (
    change_requests,
    contributions,
    managers,
    proposal_roles,
)
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import (
    ChangeRequestParts,
    ContributorSources,
    ProposalRoleEligibility,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    ResultsPublication,
    RoundLifecycleStates,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

# Held by the provider organization's owner, or by the offering's own manager.
user_can_accept_requested_offering = permission_factory(
    PermissionEnum.ACCEPT_REQUESTED_OFFERING,
    ["offering", "offering.customer"],
)

# A call's permissions can be held on the call itself, where a CALL.MANAGER role
# sits, or on its managing organisation, where a CUSTOMER.CALL_ORGANIZER role is
# bound -- both roles ship carrying CALL.UPDATE. Every gate on a call has to
# accept either, so the traversal is named once here rather than restated (and
# forgotten) per call site.
CALL_PERMISSION_SOURCES = ["*", "manager"]


def configurable_call_ids(user):
    """Ids of the calls whose configuration ``user`` may change: UPDATE_CALL
    on the call itself, on its managing organisation, or on the customer
    running that organisation. Staff and support are not special-cased here."""
    from waldur_core.structure import models as structure_models

    def scope(model):
        return get_scope_ids(
            user,
            ContentType.objects.get_for_model(model),
            permission=PermissionEnum.UPDATE_CALL,
        )

    return proposal_models.Call.objects.filter(
        Q(pk__in=scope(proposal_models.Call))
        | Q(manager_id__in=scope(proposal_models.CallManagingOrganisation))
        | Q(manager__customer_id__in=scope(structure_models.Customer))
    ).values_list("pk", flat=True)


@access.describes(
    access.any_of(
        access.permission(PermissionEnum.UPDATE_CALL, ["*", "customer"]),
        access.custom(
            "A user who may change the configuration of a call of the "
            "organization, for that call."
        ),
    ),
    needs_object=True,
)
def can_read_organisation_reports(
    request, view, obj: "proposal_models.CallManagingOrganisation | None" = None
):
    """CALL.UPDATE on the call managing organisation or its customer, or on
    one of the organisation's calls: such a call manager reports on their
    own calls only."""
    if obj is None:
        return
    if has_permission_on_any_source(
        request, PermissionEnum.UPDATE_CALL, obj, ["*", "customer"]
    ):
        return
    if proposal_models.Call.objects.filter(
        manager=obj, pk__in=configurable_call_ids(request.user)
    ).exists():
        return
    raise exceptions.PermissionDenied()


def _call_creating_organisations(user):
    """The call managing organisations ``user`` may create calls in."""
    from waldur_core.structure import models as structure_models

    organisations = get_scope_ids(
        user,
        ContentType.objects.get_for_model(proposal_models.CallManagingOrganisation),
        permission=PermissionEnum.CREATE_CALL,
    )
    customers = get_scope_ids(
        user,
        ContentType.objects.get_for_model(structure_models.Customer),
        permission=PermissionEnum.CREATE_CALL,
    )
    return proposal_models.CallManagingOrganisation.objects.filter(
        Q(pk__in=organisations) | Q(customer_id__in=customers)
    )


def may_create_call_in(request_or_user, manager) -> bool:
    """Whether the user may create a call in the call managing organisation
    ``manager``: CREATE_CALL held on the organisation itself. This is the
    rule import_call enforces on its target."""
    return has_permission(request_or_user, PermissionEnum.CREATE_CALL, manager)


def may_create_calls(user) -> bool:
    """Whether ``user`` may create a call in some call managing organisation,
    by the rule of ``may_create_call_in``: the people a published call
    template is for. CREATE_CALL held only on the customer does not count,
    as import_call would refuse it."""
    if not user.is_active:
        return False
    if user.is_staff:
        return True
    return get_scope_ids(
        user,
        ContentType.objects.get_for_model(proposal_models.CallManagingOrganisation),
        permission=PermissionEnum.CREATE_CALL,
    ).exists()


def may_configure_calls(user) -> bool:
    """Whether ``user`` configures any call, or may create one: the people a
    call's workflow configuration is for."""
    if user.is_staff or user.is_support:
        return True
    if configurable_call_ids(user).exists():
        return True
    return _call_creating_organisations(user).exists()


def form_organization_ids(user):
    """Ids of the organisations (customers) whose own forms ``user`` reads
    and attaches: those running a call ``user`` configures, and those
    ``user`` may create calls in. Staff and support are not special-cased."""
    return set(
        proposal_models.Call.objects.filter(
            pk__in=configurable_call_ids(user)
        ).values_list("manager__customer_id", flat=True)
    ) | set(_call_creating_organisations(user).values_list("customer_id", flat=True))


def forms_visible_to(user, queryset):
    """The checklists of ``queryset`` that ``user`` may see when configuring
    calls: the forms the whole portal shares and those of the user's own
    organisations. Staff and support see them all."""
    if user.is_staff or user.is_support:
        return queryset
    return queryset.filter(
        Q(organization__isnull=True)
        | Q(organization_id__in=form_organization_ids(user))
    )


def form_fits_organization(checklist, organization_id) -> bool:
    """Whether ``checklist`` may be attached to a call the organisation
    (customer) ``organization_id`` runs: a form the whole portal shares, or
    one of that organisation's own."""
    return (
        checklist is None
        or checklist.organization_id is None
        or checklist.organization_id == organization_id
    )


def oversees_as_call_team(request_or_user, proposal, drives_workflow=False) -> bool:
    """Whether the user oversees ``proposal`` for its call.

    A call manager on the call or a call organiser on its managing
    organisation: whoever may update the call (staff included, as for any
    permission), or -- with ``drives_workflow`` -- whoever drives its
    proposals through the workflow (``manages_call_workflow``; staff are the
    callers' to add). Never anyone on the proposal's own applicant team,
    staff included: whoever applies with a proposal does not evaluate it,
    read its evaluation, or change it outside what the applicant may,
    whatever they hold on the call.

    Every check of the call team's oversight of one proposal goes through
    here; ``manages_call_workflow`` alone is for what concerns the call as a
    whole, not one proposal of it.
    """
    if request_or_user is None:
        return False
    user = getattr(request_or_user, "user", request_or_user)
    if user is None or user.is_anonymous:
        return False
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if drives_workflow:
        return manages_call_workflow(user, proposal.round.call)
    return has_permission_on_any_source(
        request_or_user,
        PermissionEnum.UPDATE_CALL,
        proposal.round.call,
        CALL_PERMISSION_SOURCES,
    )


def oversees_proposal_call(request_or_user, proposal) -> bool:
    """Whether the user may update the proposal's call, as its overseer.

    Staff, a call manager on the call or a call organiser on its managing
    organisation, unless on the proposal's applicant team
    (``oversees_as_call_team``). These may change who manages a proposal at
    any time, and are the only ones besides staff who may once the proposal
    is submitted.
    """
    return oversees_as_call_team(request_or_user, proposal)


def manages_proposal_workflow(user, proposal) -> bool:
    """Whether ``user`` drives ``proposal`` through its call's workflow:
    ``manages_call_workflow`` on its call, never from its applicant team."""
    return oversees_as_call_team(user, proposal, drives_workflow=True)


def manages_call_workflow(user, call) -> bool:
    """Whether ``user`` drives the call's proposals through its workflow.

    A call manager on the call, or a call organiser on its managing
    organisation (the holders of CALL.APPROVE_AND_REJECT_PROPOSALS there, as
    homeport's workflow actions read it). They complete, reject and
    advance any step the applicant does not complete. Staff are the
    callers' to add.
    """
    if user is None or user.is_anonymous:
        return False
    if get_users(call, role_name=RoleEnum.CALL_MANAGER).filter(pk=user.pk).exists():
        return True
    return has_permission(
        user, PermissionEnum.APPROVE_AND_REJECT_PROPOSALS, call.manager
    )


def acts_for_applicant(user, proposal) -> bool:
    """Whether ``user`` acts for the applicant on ``proposal``: any of its
    current proposal managers. The other members of the team read only."""
    return is_active_proposal_manager(user, proposal)


def _holds_on_proposal(request, permission, proposal) -> bool:
    # The author keeps their rights on the proposal only while still on its
    # team: removed from it, they hold nothing on the proposal any more.
    user = request.user
    return (
        has_staff_access(request)
        or has_permission(request, permission, proposal)
        or (
            proposal.created_by_id == user.id
            and proposal_roles.is_on_applicant_team(user, proposal)
        )
    )


def can_update_proposal(request, view, obj=None):
    """Edit a proposal's own content: its managers and administrators
    (PROPOSAL.UPDATE), its author while still on its team, and staff."""
    if obj is None:
        return
    if not _holds_on_proposal(request, PermissionEnum.UPDATE_PROPOSAL, obj):
        raise exceptions.PermissionDenied()


# The part of a proposal each attachment action changes, for
# ``can_update_proposal_attachments``; the requested resources otherwise.
_ATTACHMENT_ACTION_PARTS = {
    "attach_document": ChangeRequestParts.DOCUMENTS,
    "detach_documents": ChangeRequestParts.DOCUMENTS,
}


def proposals_read_whole(user):
    """The proposals ``user`` reads all of: every one for staff and support;
    else those they read through any standing but a provider's
    (``models.reads_whole_proposal_q``), or through the organisation running
    the call (LIST_PROPOSALS) or a role on the proposal itself, as
    ``filter_queryset_for_user`` opens them."""
    queryset = proposal_models.Proposal.objects.all()
    if user.is_staff or user.is_support:
        return queryset
    return queryset.filter(_reads_whole_through_own_roles_q(user))


def _call_organisation_q(user):
    """The proposals of the calls run by an organisation in which ``user``
    may list proposals (LIST_PROPOSALS)."""
    return Q(
        round__call__manager__customer__in=get_connected_customers_by_permission(
            user, PermissionEnum.LIST_PROPOSALS
        )
    )


def _reads_whole_through_own_roles_q(user):
    return (
        proposal_models.reads_whole_proposal_q(user)
        | _call_organisation_q(user)
        | Q(
            pk__in=get_scope_ids(
                user, ContentType.objects.get_for_model(proposal_models.Proposal)
            )
        )
    )


def involved_proposals(user):
    """The proposals ``user`` takes part in: those they read through a role
    of their own -- on the proposal, its call, the organisation running the
    call, an offering it requests -- as ``filter_queryset_for_user`` opens
    them to anyone but staff and support. Staff and support read every
    proposal besides; this leaves that reach out, for the lists of the
    user's own work."""
    return proposal_models.Proposal.objects.filter(
        proposal_models.filter_proposals(user) | _call_organisation_q(user)
    )


def involved_requested_resources(user, rows):
    """``rows`` narrowed to the proposals ``user`` takes part in, and, of a
    proposal they take part in only as one of its providers, to their own
    offerings' requests -- as the list shows them to anyone but staff and
    support."""
    return rows.filter(proposal__in=involved_proposals(user)).filter(
        Q(
            proposal__in=proposal_models.Proposal.objects.filter(
                _reads_whole_through_own_roles_q(user)
            )
        )
        | Q(
            requested_offering__offering_id__in=contributions.offering_ids_with_role_of(
                user
            )
        )
    )


def offering_scope(user, proposal):
    """The offerings whose part of ``proposal`` ``user`` reads, or None when
    they read all of it.

    Whoever reads the proposal as staff, support, its applicant team, the
    call's managers and organisers, its panel, its reviewers or the holder
    of an evaluator role on it reads all of it (``proposals_read_whole``).
    Someone who reads it only as a provider -- managing an offering it
    requested or was awarded, or holding one of its per-offering slots --
    reads only what concerns their own offerings: their requested resources,
    their slots, their award and its provisioning. Never the other
    providers' requests, amounts or outcomes.
    """
    if user.is_staff or user.is_support:
        return None
    if proposals_read_whole(user).filter(pk=proposal.pk).exists():
        return None
    return frozenset(contributions.offering_ids_with_role_of(user))


def award_scope(user, proposal):
    """The offerings whose part of ``proposal``'s award ``user`` reads, or
    None when they read all of it.

    Of those reading the award (``user_can_view_awarded_resources``), staff,
    support, the call's managers and organisers and the applicant side read
    all of it; the managers of the offerings involved only their own.
    """
    if user.is_staff or user.is_support:
        return None
    if _user_manages_the_call(user, proposal) or _user_is_on_the_applicant_team(
        user, proposal
    ):
        return None
    return frozenset(contributions.offering_ids_managed_by(user))


def can_update_proposal_attachments(request, view, obj=None):
    """Change a proposal's requested resources and documents.

    Those overseeing the call, and staff, at any time. The applicant side --
    its creator, managers and administrators (PROPOSAL.UPDATE) -- only while
    the proposal is a draft, or while an open request for changes unlocks
    the documents or the resources (``change_requests.applicant_may_edit``).
    Reads stay open to everyone who can see the proposal, each narrowed by
    the view to the part they read (``offering_scope``).
    """
    if obj is None or request.method in permissions.SAFE_METHODS:
        return
    if oversees_proposal_call(request, obj):
        return
    if not _holds_on_proposal(request, PermissionEnum.UPDATE_PROPOSAL, obj):
        raise exceptions.PermissionDenied()
    part = _ATTACHMENT_ACTION_PARTS.get(
        getattr(view, "action", None), ChangeRequestParts.RESOURCES
    )
    change_requests.require_applicant_may_edit(obj, part)


def can_answer_proposal_checklist(request, view, obj=None):
    """Answer a proposal's checklist: those who may edit the proposal
    (PROPOSAL.UPDATE), only while it is a draft or an open request for
    changes unlocks its compliance answers; staff at any time."""
    if obj is None:
        return
    if not has_permission(request, PermissionEnum.UPDATE_PROPOSAL, obj):
        raise exceptions.PermissionDenied()
    if has_staff_access(request):
        return
    change_requests.require_applicant_may_edit(obj, ChangeRequestParts.COMPLIANCE)


def can_manage_proposal(request, view, obj=None):
    """Submit or delete a proposal: its managers (PROPOSAL.MANAGE), its
    author while still on its team, and staff."""
    if obj is None:
        return
    if not _holds_on_proposal(request, PermissionEnum.MANAGE_PROPOSAL, obj):
        raise exceptions.PermissionDenied()


def is_active_proposal_manager(user, proposal) -> bool:
    """Whether ``user`` holds the proposal manager role on ``proposal``."""
    if user is None:
        return False
    return permissions_models.UserRole.objects.filter(
        scope=proposal,
        user=user,
        is_active=True,
        role__name=RoleEnum.PROPOSAL_MANAGER,
    ).exists()


def support_can_read(check):
    """Let support users through `check` on safe methods only.

    Support can read every call (CallQuerySet.filter_for_user), so the data a
    call is managed with -- reviewer pool, COI and matching configuration,
    compliance -- must be readable to them too, or the call pages they are
    allowed to open come out broken. Writes still go through `check`.

    The wrapper carries `check`'s `permission` / `sources` over, since
    ActionsPermission uses `sources` to decide the check needs the object, and
    describes itself as support-for-reads or `check`.
    """

    def wrapper(request, view, scope=None):
        if (
            request.method in permissions.SAFE_METHODS
            and request.user.is_support
            and check_pat_support_scope(request)
        ):
            return
        return check(request, view, scope)

    for attr in ("permission", "sources"):
        if hasattr(check, attr):
            setattr(wrapper, attr, getattr(check, attr))
    wrapper.access_entries = [
        access.any_of(
            access.custom("Global support, for reads."), *access.describe_check(check)
        )
    ]
    wrapper.needs_object = access.needs_object(check)
    return wrapper


class CanUpdateCallPermission(permissions.BasePermission):
    """UPDATE_CALL on the object's call, its managing organisation or the
    customer running it.

    It implements only ``has_object_permission``, so it checks routes that
    look an object up and nothing else: a create or list says who may call
    it on its own.
    """

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        # Schema generation gives the view no URL arguments; detail says it
        # looks an object up.
        if not (getattr(view, "detail", False) or access.has_lookup(view)):
            return []
        return [access.permission(PermissionEnum.UPDATE_CALL, CALL_PERMISSION_SOURCES)]

    def has_object_permission(self, request, view, obj):
        call = obj.call
        return has_permission_on_any_source(
            request,
            PermissionEnum.UPDATE_CALL,
            call,
            CALL_PERMISSION_SOURCES,
        )


# Map proposal.ResponsibleRoles → permission system role on the call.
# Roles handled by special-case branches (applicant, offering_manager) are
# intentionally absent from this dict — see _user_can_act_on_active_step.
RESPONSIBLE_ROLE_TO_CALL_ROLE = {
    ResponsibleRoles.CALL_MANAGER: RoleEnum.CALL_MANAGER,
    ResponsibleRoles.REVIEWER: RoleEnum.CALL_REVIEWER,
    ResponsibleRoles.PANEL_MEMBER: RoleEnum.CALL_PANEL_MEMBER,
}


def holds_call_role(user, call) -> bool:
    """Whether ``user`` holds any role on ``call`` itself."""
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        user=user,
        content_type=ContentType.objects.get_for_model(proposal_models.Call),
        object_id=call.pk,
    ).exists()


def holds_call_role_on_proposal(user, proposal, role_name) -> bool:
    """Whether ``user`` holds the call role ``role_name`` for ``proposal``.

    A call reviewer holds it only on the proposals they hold work on: the
    reviewer role is the call's pool, not a grant on all its proposals. The
    other call roles hold it on every proposal of the call. A conflict of
    interest standing between the user and the proposal stops every role but
    the call manager's, as it stops the holder of an evaluator role (see
    ``proposal_roles.recorded_conflicts``).
    """
    call = proposal.round.call
    if not get_users(call, role_name=role_name).filter(pk=user.pk).exists():
        return False
    if (
        role_name != RoleEnum.CALL_MANAGER
        and proposal_roles.recorded_conflicts(user, proposal).exists()
    ):
        return False
    if role_name == RoleEnum.CALL_REVIEWER:
        return managers.holds_reviewer_work(user, proposal)
    return True


def panel_chair_ids(call, proposal=None) -> set[int]:
    """Ids of the call's panel chair and vice-chair who act as such.

    The one resolver for the ``panel_chair`` completer, contributor source
    and notification recipient, so the people who may act and the people who
    are told are one set. Each still holds the panel member role on the call:
    the pointers are cleared when it is revoked, and checked here as well.
    For ``proposal``, neither a chair with a conflict of interest recorded
    with it nor one on its applicant team.
    """
    ids = {call.panel_chair_id, call.panel_vice_chair_id} - {None}
    if not ids:
        return set()
    ids = set(
        get_users(call, role_name=RoleEnum.CALL_PANEL_MEMBER)
        .filter(pk__in=ids)
        .values_list("id", flat=True)
    )
    if proposal is not None and ids:
        ids -= proposal_roles.users_with_recorded_conflicts(proposal)
        ids -= proposal_roles.applicant_team_user_ids(proposal)
    return ids


def chairs_panel_for(user, proposal) -> bool:
    """Whether ``user`` is the panel chair or vice-chair of ``proposal``'s
    call, as ``panel_chair_ids`` has it."""
    if user is None or user.is_anonymous:
        return False
    return user.pk in panel_chair_ids(proposal.round.call, proposal)


def _offering_manager_user_ids(offering_ids):
    """Ids of the active managers of the offerings ``offering_ids`` names."""
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=offering_ct,
        object_id__in=offering_ids,
    ).values_list("user_id", flat=True)


offering_manager_ids = _offering_manager_user_ids


def _requested_offering_ids(proposal):
    """Accepted offerings the proposal asks for through its requests."""
    return proposal_models.RequestedResource.objects.filter(
        proposal=proposal,
        requested_offering__state=RequestedOfferingStates.ACCEPTED,
    ).values_list("requested_offering__offering_id", flat=True)


def award_is_decided(proposal) -> bool:
    """Whether the proposal's award is what its offerings provide now.

    True once the proposal is accepted, or once its allocation decision is
    completed as approved -- held for the round or not, as the award no
    longer changes after it.
    """
    if proposal.state == ProposalStates.ACCEPTED:
        return True
    return proposal.workflow_step_instances.filter(
        is_allocation_decision=True,
        status=WorkflowStepInstanceStatuses.COMPLETED,
        outcome=WorkflowStepOutcomes.APPROVED,
    ).exists()


def offering_ids_in_play(proposal):
    """Ids of the offerings whose teams a proposal involves now.

    The accepted offerings it requested until the award is decided, and the
    accepted offerings it was awarded after: the call manager may move an
    item onto another offering, whose provider then provisions it, while the
    offering it was requested on no longer has a part in it. Both sets give
    their managers read access to the proposal
    (``managers.get_offering_manager_proposals``).
    """
    if award_is_decided(proposal):
        return proposal_models.AwardedResource.objects.filter(
            proposal=proposal,
            requested_offering__state=RequestedOfferingStates.ACCEPTED,
        ).values_list("requested_offering__offering_id", flat=True)
    return _requested_offering_ids(proposal)


def offering_team_user_ids_for_proposal(proposal):
    """Ids of the managers of the offerings ``proposal`` involves now.

    The audience of a notification rule addressed to the offering teams.
    Nobody on the proposal's own team: a provider who applied hears about
    the proposal as an applicant, never as its provider.
    """
    return _offering_manager_user_ids(offering_ids_in_play(proposal)).exclude(
        user_id__in=proposal_roles.applicant_team_user_ids(proposal)
    )


def provisioned_offering_ids(proposal):
    """Offerings the proposal's award provisioned a resource on: the
    hosting entities of an accepted proposal's post-award steps."""
    return proposal_models.AwardedResource.objects.filter(
        proposal=proposal, resource__isnull=False
    ).values_list("resource__offering_id", flat=True)


def offering_manager_user_ids_for_proposal(proposal, call_step=None):
    """Ids of the offering managers who evaluate ``proposal``.

    Only the managers of accepted offerings that the proposal actually
    requested (through its ``RequestedResource`` rows) -- the offerings that
    make it visible to them in ``managers.get_offering_manager_proposals``.
    Managers of other offerings in the same call are not involved with it.
    On a step of an extension or closing (``call_step``), the managers of
    the offerings the award provisioned resources on instead: those host the
    project now, and an offering requested but not awarded has no part in
    it. Shared by the step-permission checks and the notification rules, so
    the people who may act on the step and the people mailed about it are
    one set. A conflict of interest recorded with the proposal keeps an
    offering manager off it, as it keeps a reviewer.
    """
    if call_step is not None and call_step.is_post_award:
        ids = _offering_manager_user_ids(provisioned_offering_ids(proposal))
    else:
        ids = _offering_manager_user_ids(_requested_offering_ids(proposal))
    return ids.exclude(
        user_id__in=proposal_roles.users_with_recorded_conflicts(proposal)
    )


def _user_holds_offering_manager_for_proposal(user, proposal, call_step=None):
    """True iff the user manages an offering the proposal involves at
    ``call_step``: one it requested, or after the award one hosting it."""
    return (
        offering_manager_user_ids_for_proposal(proposal, call_step)
        .filter(user=user)
        .exists()
    )


def parse_step_uuid(value):
    """The hex of the step instance uuid a request names, or None."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value)).hex
    except (TypeError, ValueError, AttributeError):
        return None


def _active_instances(proposal):
    return list(
        proposal.workflow_step_instances.select_related("call_step")
        .filter(status=WorkflowStepInstanceStatuses.ACTIVE)
        .order_by("order", "id")
    )


def acted_on_step(proposal, step_uuid):
    """The proposal's active step instance a step action is about.

    The one ``step_uuid`` names among the active ones. Without a name, the
    one active step, if only one is: when several steps of a stage run at
    once, an action that does not say which it is about is about none.
    """
    active = _active_instances(proposal)
    named = parse_step_uuid(step_uuid)
    if named is not None:
        return next((i for i in active if i.uuid.hex == named), None)
    if len(active) == 1:
        return active[0]
    return None


def _acts_as_staff(user, staff) -> bool:
    """`staff` is whether the request acts as staff (`has_staff_access`), so
    that a personal access token without the staff scope does not; it
    defaults to the user's flag."""
    return user.is_staff if staff is None else staff


def _user_can_act_on_active_step(user, proposal, step_uuid=None, staff=None):
    """True iff the user may act on the proposal's active step ``step_uuid``
    names.

    Staff bypass this check. When no step is active, access is allowed and
    the view's own state/concurrency guards take over. The step is the
    active one the request names; a request naming none is about the one
    active step, and is refused when several run at once. One naming a step
    that is not active -- moved on meanwhile, or not reached yet -- is
    allowed only to whoever may act on that very step, so the view tells
    them to refresh; acting on another active step is no standing on it.
    The view checks again under its lock (``user_may_act_on_locked_step``).
    """
    if _acts_as_staff(user, staff):
        return True
    active = _active_instances(proposal)
    if not active:
        # No active step — let the view's own guards return the right error.
        return True
    named = parse_step_uuid(step_uuid)
    if named is not None:
        instance = next((i for i in active if i.uuid.hex == named), None)
        if instance is None:
            instance = (
                proposal.workflow_step_instances.select_related(
                    "call_step", "proposal__round__call"
                )
                .filter(uuid=named)
                .first()
            )
        return instance is not None and user_can_act_on_step(user, instance)
    if len(active) > 1:
        return False
    return user_can_act_on_step(user, active[0])


def user_can_act_on_step(user, instance):
    """True iff the user has the role required by the proposal's step
    ``instance``: the step itself, never another one running beside it.

    Staff bypass this check. A step without a completer configured is the
    call's managers' alone: nobody else is named to act on it.
    """
    if user.is_staff:
        return True

    proposal = instance.proposal
    call_step = instance.call_step
    role = call_step.completer
    if role == ResponsibleRoles.APPLICANT:
        # An applicant step (e.g. the award response) is the applicant's to
        # complete -- any of the proposal's managers acts for the team; the
        # call manager does not act on the applicant's behalf.
        return acts_for_applicant(user, proposal)

    # Call managers and organisers drive step progression: they may
    # complete/reject any non-applicant step. The step's review/score gates
    # (_enforce_step_gates) remain the guardrail, so this is not a rubber
    # stamp. The step's own responsible actor (reviewer / panel member /
    # offering manager) may also act on it, unless they are on the applicant
    # team -- a call manager included: nobody decides a step of a proposal
    # they apply with.
    if manages_proposal_workflow(user, proposal):
        return True
    if not role or proposal_roles.is_on_applicant_team(user, proposal):
        return False

    # Otherwise the step's own completer. An unrecognised completer fails
    # closed; on a lead-only step completed by its contributing role, only
    # that role's lead acts.
    return user_completes_step(user, proposal, call_step)


user_can_act_on_active_step = _user_can_act_on_active_step


def user_may_act_on_locked_step(user, instance, rejecting=False):
    """Whether ``user`` may complete -- or, with ``rejecting``, reject at --
    the step ``instance``, read under the proposal's lock.

    The permission check of a step action runs before the lock; by the time
    the lock is held another request may have moved the proposal on, or the
    user's standing on it may have changed. The action is about the step
    that is active now, so it is checked again against that step. The
    applicant does not reject their own proposal at a step of theirs.
    """
    if user.is_staff:
        return True
    if not user_can_act_on_step(user, instance):
        return False
    if rejecting and instance.call_step.completer == ResponsibleRoles.APPLICANT:
        return False
    return True


# Call roles that grant no call-wide read of the proposals' step internals:
# the reviewer pool and the panel read those of the steps they take part in.
STEP_SCOPED_CALL_ROLES = frozenset({RoleEnum.CALL_REVIEWER, RoleEnum.CALL_PANEL_MEMBER})


def user_can_view_internal_notes(
    user, proposal, instance=None, access=None, staff=None
):
    """True iff the user reads the call team's side of the proposal's steps.

    That side is a step's internal notes, who completed it, and the outcome
    and reason the step list masks from readers outside the call team (a
    review verdict while the call keeps reviews private, feedback withheld
    until its release).

    Applicants never see it (the entire point of internal notes is to be
    hidden from them). Staff always do. A call manager, or any other call
    role granted call-wide, reads it on every step of the call's proposals,
    including steps that are already past. A call reviewer or panel member
    reads it only on ``instance`` -- one of the proposal's step instances --
    when they take part in that step: complete it or contribute to it
    through their call role or an evaluator role, or hold a later step that
    reads it as an input (``contributions.takes_part_in_step``). Without an
    ``instance`` they are not the call team.

    The whole applicant team is denied, not only the proposal's author: a
    member of the team who also sits on the call's panel reads their own
    proposal as an applicant.

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
    if staff is None and access is not None:
        staff = access.staff
    if _acts_as_staff(user, staff):
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    call_ct = ContentType.objects.get_for_model(proposal_models.Call)
    held = set(
        permissions_models.UserRole.objects.filter(
            is_active=True,
            user=user,
            content_type=call_ct,
            object_id=proposal.round.call_id,
        ).values_list("role__name", flat=True)
    )
    if not held:
        return False
    if held - STEP_SCOPED_CALL_ROLES:
        return True
    if instance is None:
        return False
    return contributions.takes_part_in_step(user, instance, access)


def reads_as_reviewer_only(user, proposal) -> bool:
    """Whether ``user`` reads ``proposal`` solely as one of its reviewers.

    A reviewer -- with or without the call's reviewer role, which is the
    reviewer pool -- holding work on the proposal and no other reason to read
    it: not on its team, not overseeing the call, not on its panel, not one of
    its offering managers, not an evaluator role holder. Such a reader sees
    the outcome and reason only of the steps they take part in.
    """
    if user.is_staff or user.is_support:
        return False
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    call = proposal.round.call
    if oversees_proposal_call(user, proposal) or manages_proposal_workflow(
        user, proposal
    ):
        return False
    call_roles = set(
        permissions_models.UserRole.objects.filter(
            is_active=True,
            user=user,
            content_type=ContentType.objects.get_for_model(proposal_models.Call),
            object_id=call.pk,
        ).values_list("role__name", flat=True)
    )
    if call_roles - {RoleEnum.CALL_REVIEWER}:
        return False
    if _user_holds_offering_manager_for_proposal(user, proposal):
        return False
    if proposal_roles.holds_role(user, proposal):
        return False
    return managers.holds_reviewer_work(user, proposal)


def user_can_manage_held_decision(request_or_user, proposal, staff=None):
    """True iff the user may change a decision held for the round's publication.

    Staff, and whoever holds CALL.UPDATE on the call or on its managing
    organisation. Not support, who only read. The applicant team is refused
    even when they also manage the call, as for internal notes.

    Given a request, a personal access token's scopes and bindings apply to
    the CALL.UPDATE check, and ``staff`` defaults to ``has_staff_access``.
    """
    if isinstance(request_or_user, User):
        user = request_or_user
    else:
        user = request_or_user.user
        if staff is None:
            staff = has_staff_access(request_or_user)
    if _acts_as_staff(user, staff):
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    return oversees_proposal_call(request_or_user, proposal)


def user_can_view_held_decisions_of_call(user, call):
    """True iff the user may learn which decisions of the call are held.

    The call-level counterpart of ``user_can_view_held_decision`` for what a
    round reports about all its proposals: staff, support and whoever holds
    CALL.UPDATE on the call or on its managing organisation.
    """
    if user.is_staff or user.is_support:
        return True
    return has_permission_on_any_source(
        user, PermissionEnum.UPDATE_CALL, call, CALL_PERMISSION_SOURCES
    )


def user_can_view_round_adoption(user, call_round):
    """True iff the user may read how the round's outcome was adopted.

    The adoption note and document, and the reason results were published
    with decisions missing, state the round's outcome. While the round holds
    its decisions back only those who may see held decisions read them; once
    its results are out (or for a call announcing each decision as it is
    made) everyone who sees the round does.
    """
    return not call_round.withholds_results or user_can_view_held_decisions_of_call(
        user, call_round.call
    )


def user_can_view_held_decision(user, proposal):
    """True iff the user may see a decision held for the round's publication.

    Narrower than ``user_can_view_internal_notes``: until the round publishes,
    the outcome is the call managers' alone -- reviewers, panel members and
    offering managers on the call learn it with everyone else. Staff and
    support see it, as they see everything of a call. The applicant is denied
    even when they also manage the call, as for internal notes.
    """
    return user.is_support or user_can_manage_held_decision(user, proposal)


def calls_managed_by(user):
    """Calls the user holds CALL.UPDATE on, on the call or its managing organisation.

    Unlike ``filter_queryset_for_user``, being staff or support grants nothing
    here, and listing a call (reviewers, organisation owners) is not managing it.
    """
    if not user.is_authenticated or not user.is_active:
        return proposal_models.Call.objects.none()
    managed_calls = get_scope_ids(
        user,
        ContentType.objects.get_for_model(proposal_models.Call),
        permission=PermissionEnum.UPDATE_CALL,
    )
    organised_calls = get_scope_ids(
        user,
        ContentType.objects.get_for_model(proposal_models.CallManagingOrganisation),
        permission=PermissionEnum.UPDATE_CALL,
    )
    return proposal_models.Call.objects.filter(
        Q(id__in=managed_calls) | Q(manager_id__in=organised_calls)
    )


def proposals_with_held_decisions_hidden_from(user):
    """Proposals whose round still withholds its results from the user.

    ``user_can_view_held_decision`` over every proposal at once, in SQL: those
    of a round that holds its decisions back, unless the user is staff or
    support, or holds CALL.UPDATE on the call or its managing organisation
    without being on the proposal's applicant team. ``None`` when nothing is
    hidden.
    """
    if user.is_active and (user.is_staff or user.is_support):
        return None
    withheld = proposal_models.Proposal.objects.filter(
        round__call__publish_results=ResultsPublication.WITH_ROUND
    ).exclude(round__lifecycle_state__in=RoundLifecycleStates.PUBLISHED)
    if not user.is_active:
        return withheld
    managed_calls = get_scope_ids(
        user,
        ContentType.objects.get_for_model(proposal_models.Call),
        permission=PermissionEnum.UPDATE_CALL,
    )
    organised_calls = get_scope_ids(
        user,
        ContentType.objects.get_for_model(proposal_models.CallManagingOrganisation),
        permission=PermissionEnum.UPDATE_CALL,
    )
    return withheld.exclude(
        ~Q(pk__in=proposal_roles.applicant_team_proposal_ids(user))
        & (
            Q(round__call_id__in=managed_calls)
            | Q(round__call__manager_id__in=organised_calls)
        )
    )


def calls_hiding_round_events_from(user):
    """Calls whose round events ``user`` may not read on the call's feed.

    A round's history -- who opened a step for it, wrote its report, decided
    it in bulk -- is the call team's: staff and support, the call's
    managers, and whoever configures the call or drives its workflow on its
    managing organisation. Anyone else who sees the call reads none of it.
    ``None`` when nothing is hidden.
    """
    if user.is_active and (user.is_staff or user.is_support):
        return None
    calls = proposal_models.Call.objects.all()
    if not user.is_active:
        return calls
    call_ct = ContentType.objects.get_for_model(proposal_models.Call)
    organisation_ct = ContentType.objects.get_for_model(
        proposal_models.CallManagingOrganisation
    )
    return calls.exclude(
        Q(pk__in=configurable_call_ids(user))
        | Q(pk__in=get_scope_ids(user, call_ct, RoleEnum.CALL_MANAGER))
        | Q(
            manager_id__in=get_scope_ids(
                user,
                organisation_ct,
                permission=PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
            )
        )
    )


def can_reopen_held_decision(request, view, obj=None):
    """ActionsPermission check for reopening a held allocation decision."""
    if obj is None:
        return
    if not user_can_manage_held_decision(request, obj):
        raise exceptions.PermissionDenied(
            "Only the call's managers can reopen a held decision."
        )


can_reopen_held_decision.sources = ["*"]


def _requested_step_uuid(request):
    data = getattr(request, "data", None)
    if hasattr(data, "get"):
        return data.get("step_uuid")
    return None


def user_can_set_proposal_panel(user, proposal) -> bool:
    """Whether ``user`` may move ``proposal`` to another panel.

    Staff; whoever may update its call; and the completer of the
    proposal's running assignment step that assigns a role held through the
    proposal's panel -- the call's panel chair in Regular Access. Never
    someone on the applicant team.
    """
    if user.is_staff:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if oversees_proposal_call(user, proposal):
        return True
    for instance in _active_instances(proposal):
        call_step = instance.call_step
        if call_step.kind != StepKinds.ASSIGNMENT:
            continue
        if not call_step.assigns_roles.filter(
            eligible_from__in=ProposalRoleEligibility.PANEL_BASED
        ).exists():
            continue
        if user_completes_step(user, proposal, call_step):
            return True
    return False


def can_set_proposal_panel(request, view, obj=None):
    """Staff, whoever may update the proposal's call, and the completer of its
    running panel-based assignment step move the proposal to another panel;
    never the applicant team (``user_can_set_proposal_panel``)."""
    if obj is None:
        return
    if not user_can_set_proposal_panel(request.user, obj):
        raise exceptions.PermissionDenied(
            "Only the call's managers and the completer of the proposal's "
            "panel assignment step can change its panel."
        )


can_set_proposal_panel.sources = ["*"]


def user_can_view_proposal_panel(user, proposal) -> bool:
    """Whether ``user`` sees which panel evaluates ``proposal``: whoever sees
    who evaluates it (``user_can_view_proposal_roles``), never the applicant
    team."""
    return user_can_view_proposal_roles(user, proposal)


def can_read_call_panels(request, view, obj=None):
    """Read a call's panels: staff and support, whoever may update the call,
    and the call's panel members. Not its reviewers, nor anyone else with a
    role on the call: who sits on which panel is the panel's own business."""
    if obj is None or request.method not in ("GET", "HEAD", "OPTIONS"):
        return
    user = request.user
    if user.is_staff or user.is_support:
        return
    if has_permission_on_any_source(
        request, PermissionEnum.UPDATE_CALL, obj, CALL_PERMISSION_SOURCES
    ):
        return
    if get_users(obj, role_name=RoleEnum.CALL_PANEL_MEMBER).filter(pk=user.pk).exists():
        return
    raise exceptions.PermissionDenied("You cannot see the call's panels.")


can_read_call_panels.sources = ["*"]


def can_act_on_active_workflow_step(request, view, obj=None):
    """ActionsPermission check for complete/reject workflow step actions,
    on the active step the request names (``step_uuid``)."""
    if obj is None:
        return
    if not _user_can_act_on_active_step(
        request.user,
        obj,
        _requested_step_uuid(request),
        staff=has_staff_access(request),
    ):
        raise exceptions.PermissionDenied(
            "You don't hold the role required to act on the active workflow step."
        )


# Mark check as needing the object so ActionsPermission fetches it.
can_act_on_active_workflow_step.sources = ["*"]


def can_reject_at_active_workflow_step(request, view, obj=None):
    """ActionsPermission check for rejecting a proposal at its active step.

    As acting on the step, except that the applicant does not reject their
    own proposal at a step of theirs, such as the response to reviews: a
    rejection is the call's decision. Staff still may.
    """
    if obj is None:
        return
    can_act_on_active_workflow_step(request, view, obj)
    user = request.user
    if user.is_staff:
        return
    active = acted_on_step(obj, _requested_step_uuid(request))
    if active is not None and active.call_step.completer == ResponsibleRoles.APPLICANT:
        raise exceptions.PermissionDenied(
            "The applicant cannot reject their own proposal at a step of theirs."
        )


can_reject_at_active_workflow_step.sources = ["*"]


def completes_round_step(user, call, call_step) -> bool:
    """Whether ``user`` completes the round step ``call_step`` of ``call``
    for the round as its completer, without managing the call: the call's
    panel chair or vice-chair for a step they complete, or a holder of the
    call role that completes it. Each proposal is checked again when it is
    completed (``user_can_act_on_step``)."""
    if user is None or user.is_anonymous or not call_step.round_scoped:
        return False
    role = call_step.completer
    if role == ResponsibleRoles.PANEL_CHAIR:
        return user.pk in panel_chair_ids(call)
    if role == ResponsibleRoles.PROPOSAL_ROLE:
        return proposal_models.ProposalRoleAssignment.objects.filter(
            user=user,
            role_id=call_step.completer_proposal_role_id,
            proposal__round__call=call,
            revoked_at__isnull=True,
        ).exists()
    role_name = RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if not role_name or role_name == RoleEnum.CALL_MANAGER:
        return False
    return get_users(call, role_name=role_name).filter(pk=user.pk).exists()


def can_complete_workflow_step_round(request, view, obj=None):
    """The call's managers complete any step for a round; the completer of a
    round step completes it for the proposals of the round they act on."""
    if obj is None:
        return
    user = request.user
    if user.is_staff or has_permission_on_any_source(
        request, PermissionEnum.UPDATE_CALL, obj, CALL_PERMISSION_SOURCES
    ):
        return
    data = request.data if hasattr(request.data, "get") else {}
    steps = proposal_models.CallWorkflowStep.objects.filter(call=obj)
    step_uuid = parse_step_uuid(data.get("step_uuid"))
    if step_uuid is not None:
        call_step = steps.filter(uuid=step_uuid).first()
    elif data.get("step"):
        call_step = steps.filter(key=str(data.get("step"))).first()
    else:
        call_step = None
    if call_step is None or not completes_round_step(user, obj, call_step):
        raise exceptions.PermissionDenied(
            "Only the call's managers, or the completer of a step run once "
            "per round, complete a step for the round."
        )


can_complete_workflow_step_round.sources = ["*"]


def can_advance_workflow_step(request, view, obj=None):
    """Only call managers and organisers (or staff) may manually advance a
    workflow."""
    if obj is None:
        return
    user = request.user
    if has_staff_access(request):
        return
    if not manages_proposal_workflow(user, obj):
        raise exceptions.PermissionDenied(
            "Only the call manager can advance this workflow step."
        )


can_advance_workflow_step.sources = ["*"]


def user_can_request_changes(user, proposal, call_step) -> bool:
    """Whether ``user`` may send ``proposal`` back to its applicant from
    ``call_step``: staff, and whoever acts on the step -- the call's
    managers and organisers, and its completer -- unless on the proposal's
    applicant team. Whether the step allows it is the workflow's to say."""
    if user.is_staff:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if manages_proposal_workflow(user, proposal):
        return True
    return user_completes_step(user, proposal, call_step)


def user_oversees_contributions(user, proposal) -> bool:
    """Whether ``user`` may reopen a submitted contribution on ``proposal``,
    or call a request for changes off: staff, and the call's managers and
    organisers who are not on its applicant team. A step's completer may
    not: one centre does not reopen another's assessment."""
    if user.is_staff:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    return manages_proposal_workflow(user, proposal)


def user_reads_change_request(user, change_request, access=None) -> bool:
    """Whether ``user`` reads ``change_request`` and its message.

    Staff, support and those overseeing the call's workflow read every one.
    A request to the applicant is also the applicant team's and the step's
    completers'; a reopened contribution its holders' and the person it is
    handed to -- never the applicant's, as an assessment is internal.
    """
    if user.is_staff or user.is_support:
        return True
    proposal = change_request.proposal
    on_team = proposal_roles.is_on_applicant_team(user, proposal)
    if change_request.contribution_id is None:
        if on_team:
            return True
        if manages_proposal_workflow(user, proposal):
            return True
        return user_completes_step(
            user, proposal, change_request.step_instance.call_step
        )
    if on_team:
        return False
    if manages_proposal_workflow(user, proposal):
        return True
    access = access or contributions.Access(user)
    contribution = change_request.contribution
    # The person the slot is handed to revises it; its holders keep reading
    # it, as they read the slot itself.
    return access.is_filler(contribution) or access.owns_slot(contribution)


def get_step_query_params(request):
    """``(step_uuid, step key)`` naming a call step in the query string.

    ``?step_uuid=`` names the call step itself; ``?step=<key>`` names it by
    key, the only way before a key could repeat across kinds.
    """
    query_params = getattr(request, "query_params", None) or request.GET
    return query_params.get("step_uuid"), query_params.get("step")


def require_step_query_param(request):
    step_uuid, step_key = get_step_query_params(request)
    if not step_uuid and not step_key:
        raise exceptions.ValidationError(
            {"step": "The step or step_uuid query parameter is required."}
        )
    return step_uuid, step_key


def resolve_call_step(proposal, step_uuid=None, step_key=None):
    """The proposal's call step named by uuid or key, or None."""
    steps = proposal_models.CallWorkflowStep.objects.filter(
        call_id=proposal.round.call_id
    )
    if step_uuid:
        try:
            return steps.filter(uuid=step_uuid).first()
        except (ValueError, DjangoValidationError):
            return None
    if step_key:
        return steps.filter(key=step_key).first()
    return None


def _resolve_step_checklist(proposal, step_uuid, step_key):
    """Return the named call step if it has a checklist attached, else None."""
    call_step = resolve_call_step(proposal, step_uuid, step_key)
    if call_step is None or not call_step.checklist_id:
        return None
    return call_step


def user_holds_step_role(user, proposal, call_step):
    """True iff the user holds the step's responsible role — a proposal
    manager, acting for the applicant, on an applicant-owned step, the
    offering manager for an offering-manager step, or the matching call role
    for reviewer/panel steps. Nobody on the applicant team holds an evaluator
    step's role. Does NOT grant staff or call-manager access (callers add
    those) and does NOT check whether the step is active. Shared by the
    answer- and read-permission checks so they stay consistent. A call
    reviewer holds a reviewer step only on a proposal they hold work on.
    """
    role = call_step.completer
    if role == ResponsibleRoles.APPLICANT:
        return acts_for_applicant(user, proposal)
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if role == ResponsibleRoles.OFFERING_MANAGER:
        return _user_holds_offering_manager_for_proposal(user, proposal, call_step)
    if role == ResponsibleRoles.PROPOSAL_ROLE:
        return proposal_roles.holds_role(
            user, proposal, call_step.completer_proposal_role_id
        )
    if role == ResponsibleRoles.PANEL_CHAIR:
        return chairs_panel_for(user, proposal)
    role_name = RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if not role_name:
        return False
    return holds_call_role_on_proposal(user, proposal, role_name)


def user_completes_step(user, proposal, call_step):
    """True iff the user holds the step's responsible role as its completer.

    ``user_holds_step_role``, except on a lead-only step completed by the
    evaluator role whose lead contributes to it: there the role's other
    holders work on the step and read it, but only its current lead
    completes it, rejects the proposal at it, answers its checklist and
    releases its report (``proposal_roles.completer_is_lead_only``). Like
    ``user_holds_step_role`` it grants nothing to staff or the call's
    managers and does not check that the step is active. Every "may
    complete this step" decision asks it, so the actions the UI offers and
    the ones the API accepts stay one set.
    """
    if call_step.completer == ResponsibleRoles.PROPOSAL_ROLE:
        if proposal_roles.is_on_applicant_team(user, proposal):
            return False
        return proposal_roles.completes_step(user, proposal, call_step)
    return user_holds_step_role(user, proposal, call_step)


def user_can_manage_proposal_role(user, proposal, role) -> bool:
    """Whether ``user`` may assign, revoke or move the lead of ``role`` on
    ``proposal``.

    Staff, and those overseeing the call, at any time; the completer of an
    active assignment step for the role while it runs. Never someone on the
    applicant team, whatever else they hold: applicants do not pick their
    evaluators.
    """
    if user.is_staff:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if oversees_proposal_call(user, proposal):
        return True
    instance = proposal_roles.active_assignment_step(proposal, role)
    if instance is None:
        return False
    return user_holds_step_role(user, proposal, instance.call_step)


def user_changes_any_holder(user, proposal) -> bool:
    """Whether ``user`` may change any holder of the proposal's evaluator
    roles, and its lead: staff, and those overseeing the call while not on
    the applicant team.

    Whoever else may manage a role -- the completer of a running assignment
    step, often one of the panel or a role holder themselves -- is a peer of
    the people they assign: they may volunteer, add holders and give up
    their own role, but the lead moves only when the current lead hands it
    over (``may_move_lead``), and only those overseeing revoke someone else
    (``may_revoke_holder``).
    """
    if user.is_staff:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    return oversees_proposal_call(user, proposal)


def may_move_lead(user, proposal, role, to_assignment=None) -> bool:
    """Whether ``user`` may make someone the lead of ``role`` on ``proposal``.

    Those changing any holder always. A peer only while the role has no lead,
    or when they are its lead handing it over -- never taking it from someone
    else. ``to_assignment`` is the holder made the lead, when already one.
    The caller holds the role's holder locks.
    """
    if user_changes_any_holder(user, proposal):
        return True
    lead = (
        proposal_roles.active_assignments(proposal, role).filter(is_lead=True).first()
    )
    return (
        lead is None
        or lead.user_id == user.id
        or (to_assignment is not None and lead.pk == to_assignment.pk)
    )


def may_revoke_holder(user, proposal, assignment) -> bool:
    """Whether ``user`` may take ``assignment`` away: those changing any
    holder, and a peer their own."""
    return user_changes_any_holder(user, proposal) or assignment.user_id == user.id


def user_can_view_proposal_roles(user, proposal) -> bool:
    """Whether ``user`` may see who holds the evaluator roles on ``proposal``.

    Staff and support, those overseeing the call, the holders themselves and
    the completer of an active assignment step. Never the applicant team.
    """
    if user.is_staff or user.is_support:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if oversees_proposal_call(user, proposal):
        return True
    if proposal_roles.holds_role(user, proposal):
        return True
    instance = proposal_roles.active_assignment_step(proposal)
    return instance is not None and user_holds_step_role(
        user, proposal, instance.call_step
    )


def can_view_proposal_roles(request, view, obj=None):
    """Who holds the evaluator roles on the proposal is read by users with the
    staff or support flag, those overseeing its call, the holders themselves
    and the completer of an active assignment step; never by the applicant
    team (``user_can_view_proposal_roles``)."""
    if obj is None:
        return
    if not user_can_view_proposal_roles(request.user, obj):
        raise exceptions.PermissionDenied("You cannot see who evaluates this proposal.")


can_view_proposal_roles.sources = ["*"]


def user_can_answer_step_checklist(user, proposal, call_step, staff=None):
    """True iff the user may submit answers to a workflow step's checklist.

    Staff and the call's managers and organisers may always answer.
    Otherwise the step must be currently ACTIVE (checklists become read-only
    once the step completes) and the user must complete the step
    (``user_completes_step``). Mirrors ``_user_can_act_on_active_step`` so answering and acting
    stay consistent.
    """
    if _acts_as_staff(user, staff):
        return True
    if manages_proposal_workflow(user, proposal):
        return True

    is_active = proposal.workflow_step_instances.filter(
        call_step=call_step,
        status=WorkflowStepInstanceStatuses.ACTIVE,
    ).exists()
    if not is_active:
        return False

    return user_completes_step(user, proposal, call_step)


def user_can_read_step_checklist(user, proposal, call_step, staff=None):
    """True iff the user may read a workflow step's checklist (its answers).

    Staff, the call's managers and organisers, and the step's responsible
    role — the latter even after the step completes, so responsible roles keep
    read access to what they submitted. On an applicant-owned step the whole
    applicant team reads it, though only its managers answer. Crucially the
    applicant may read ONLY an applicant-owned step (their submission
    checklist); they can NOT read an evaluation step's answers here — that
    would bypass the ``applicant_visible`` gate enforced on the threaded
    responses endpoint.
    """
    if _acts_as_staff(user, staff):
        return True
    if manages_proposal_workflow(user, proposal):
        return True
    if call_step.completer == ResponsibleRoles.APPLICANT:
        return proposal_roles.is_on_applicant_team(user, proposal)
    return user_holds_step_role(user, proposal, call_step)


def get_contribution_query_param(request):
    query_params = getattr(request, "query_params", None) or request.GET
    return query_params.get("contribution_uuid")


def step_instance_for(proposal, call_step):
    """The proposal's instance of ``call_step``, or None before submission.

    A step of an extension runs once per extension: its latest instance."""
    return (
        proposal.workflow_step_instances.select_related("call_step", "proposal")
        .filter(call_step=call_step)
        .order_by("-id")
        .first()
    )


def _named_contribution(request, proposal, call_step):
    """The contribution named by ``?contribution_uuid=``, None if none is named.

    Raises PermissionDenied for a name that is not a contribution of the step.
    """
    contribution_uuid = get_contribution_query_param(request)
    if not contribution_uuid:
        return None
    instance = step_instance_for(proposal, call_step)
    contribution = (
        contributions.resolve_contribution(
            request.user, instance, contribution_uuid=contribution_uuid
        )
        if instance is not None
        else None
    )
    if contribution is None:
        raise exceptions.PermissionDenied("No such contribution to this workflow step.")
    return contribution


def _may_contribute_to(user, proposal, call_step) -> bool:
    instance = step_instance_for(proposal, call_step)
    return instance is not None and contributions.may_contribute(user, instance)


def can_read_step_checklist(request, view, obj=None):
    """ActionsPermission check for the step_checklist GET action."""
    if obj is None:
        return
    step_uuid, step_key = require_step_query_param(request)
    call_step = _resolve_step_checklist(obj, step_uuid, step_key)
    if call_step is None:
        raise exceptions.PermissionDenied(
            "No checklist is configured for this workflow step."
        )
    contribution = _named_contribution(request, obj, call_step)
    if contribution is not None:
        allowed = contributions.user_can_read(request.user, contribution)
    else:
        allowed = user_can_read_step_checklist(
            request.user, obj, call_step, has_staff_access(request)
        ) or _may_contribute_to(request.user, obj, call_step)
    if not allowed:
        raise exceptions.PermissionDenied(
            "You do not have permission to view this step's checklist."
        )


can_read_step_checklist.sources = ["*"]


def can_submit_step_checklist_answers(request, view, obj=None):
    """ActionsPermission check for the submit_step_checklist_answers action."""
    if obj is None:
        return
    step_uuid, step_key = require_step_query_param(request)
    call_step = _resolve_step_checklist(obj, step_uuid, step_key)
    if call_step is None:
        raise exceptions.PermissionDenied(
            "No checklist is configured for this workflow step."
        )
    contribution = _named_contribution(request, obj, call_step)
    if contribution is not None:
        allowed = contributions.user_can_fill(request.user, contribution)
    elif call_step.collects_contributions:
        # The form of a step with contributors is theirs, each on their own
        # contribution: the call team or staff answering it on the step would
        # write, say, a consensus report in the place of whoever holds the
        # role that writes it.
        instance = step_instance_for(obj, call_step)
        allowed = (
            instance is not None
            and instance.status == WorkflowStepInstanceStatuses.ACTIVE
            and contributions.may_contribute(request.user, instance)
        )
        if not allowed:
            raise exceptions.PermissionDenied(
                "This step's form is answered on its contributions, by those "
                "who contribute to it."
            )
    else:
        instance = step_instance_for(obj, call_step)
        allowed = (
            user_can_answer_step_checklist(
                request.user, obj, call_step, has_staff_access(request)
            )
            or (
                instance is not None
                and instance.status == WorkflowStepInstanceStatuses.ACTIVE
                and contributions.may_contribute(request.user, instance)
            )
            or (
                # Changes asked of the team in a post-award run reopen the
                # answers of its applicant step to the team.
                instance is not None
                and acts_for_applicant(request.user, obj)
                and contributions.run_form_unlocked(instance)
            )
        )
    if not allowed:
        raise exceptions.PermissionDenied(
            "You cannot answer this step's checklist, or the step is no longer active."
        )


can_submit_step_checklist_answers.sources = ["*"]


def can_view_step_checklist_responses(request, view, obj=None):
    """ActionsPermission check for the threaded responses of a step's form.

    Staff and support, the call's managers, and a completer of the step who
    is not one of its contributors read every contribution. The applicant
    team only on a step configured ``applicant_visible``. The
    offering teams of a technical assessment read their peers' work unless
    it is a blind review or split per requested offering, and otherwise
    their own. Reviewers do not use this view: each reads their own
    contribution through the step's form. Which contributions each caller
    sees is narrowed further by the view.
    """
    if obj is None:
        return
    step_uuid, step_key = require_step_query_param(request)
    call_step = _resolve_step_checklist(obj, step_uuid, step_key)
    if call_step is None:
        raise exceptions.PermissionDenied(
            "No checklist is configured for this workflow step."
        )
    user = request.user
    access = contributions.access_for(request)
    instance = step_instance_for(obj, call_step)
    if access.oversees(obj) or user.is_support:
        return
    if instance is not None and access.reads_all(instance):
        return
    if access.on_applicant_team(obj):
        if call_step.applicant_visible:
            return
    elif call_step.contributor_source_kind != ContributorSources.ASSIGNED_REVIEWERS:
        if contributions.peers_read_each_other(call_step) and (
            _user_holds_offering_manager_for_proposal(user, obj, call_step)
            or (instance is not None and access.may_contribute(instance))
        ):
            return
        if (
            instance is not None
            and call_step.contributor_source_kind
            == ContributorSources.REQUESTED_OFFERING_TEAM
            and contributions.fillable_contributions(user, instance).exists()
        ):
            return
    if instance is not None and (
        access.input_side(instance) is not None
        or (
            call_step.kind == StepKinds.APPLICANT_ACTION
            and access.on_applicant_team(obj)
        )
    ):
        # Those reading the step as the input of a later one, and the
        # applicant team reading its own answer; the view narrows the list.
        return
    raise exceptions.PermissionDenied(
        "You do not have permission to view technical assessment responses."
    )


can_view_step_checklist_responses.sources = ["*"]


def _user_is_call_manager(user, proposal) -> bool:
    """A call manager on the proposal's call, not on its applicant team."""
    return (
        get_users(proposal.round.call, role_name=RoleEnum.CALL_MANAGER)
        .filter(pk=user.pk)
        .exists()
    ) and not proposal_roles.is_on_applicant_team(user, proposal)


def _user_manages_an_offering_of(user, proposal) -> bool:
    """True iff the user manages an offering the proposal requested or was awarded."""
    offering_ids = set(
        proposal_models.RequestedResource.objects.filter(proposal=proposal).values_list(
            "requested_offering__offering_id", flat=True
        )
    ) | set(
        proposal_models.AwardedResource.objects.filter(proposal=proposal).values_list(
            "requested_offering__offering_id", flat=True
        )
    )
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        user=user,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=offering_ct,
        object_id__in=offering_ids,
    ).exists()


def awarded_resources_released(proposal) -> bool:
    """Whether the award has been decided and may be shown to the applicant.

    Never while the call manager is still drafting it, nor after the allocation
    decision declined the proposal. Once approved: when the proposal has been
    accepted (its resources are provisioned in the applicant's project anyway),
    when the applicant is asked to accept or decline the award, or -- for a
    decision parked awaiting a manual advance -- when the call exposes the
    allocation step to applicants (``applicant_visible``). Never while the
    decision is held for the round's publication: until then, to the
    applicant it is still being made.
    """
    if proposal.state == ProposalStates.ACCEPTED:
        return True
    if proposal.decision_held:
        return False
    instances = list(
        proposal.workflow_step_instances.select_related("call_step").order_by(
            "order", "id"
        )
    )
    decision = next(
        (
            instance
            for instance in instances
            if instance.is_allocation_decision
            and instance.status != WorkflowStepInstanceStatuses.SKIPPED
        ),
        None,
    )
    if (
        decision is None
        or decision.status != WorkflowStepInstanceStatuses.COMPLETED
        or decision.outcome != WorkflowStepOutcomes.APPROVED
    ):
        return False
    # The applicant is asked about the award once a step of theirs after the
    # decision has started.
    after_decision = instances[instances.index(decision) + 1 :]
    if any(
        instance.call_step.completer == ResponsibleRoles.APPLICANT
        and instance.status
        not in (
            WorkflowStepInstanceStatuses.PENDING,
            WorkflowStepInstanceStatuses.SKIPPED,
        )
        for instance in after_decision
    ):
        return True
    return decision.call_step.applicant_visible


def _user_manages_the_call(user, proposal) -> bool:
    """Call manager of the proposal's call, or organiser of its managing org,
    not on its applicant team."""
    return oversees_proposal_call(user, proposal)


def _user_is_on_the_applicant_team(user, proposal) -> bool:
    """On the proposal's team, or on the project it was granted."""
    if proposal_roles.is_on_applicant_team(user, proposal):
        return True
    return proposal.project_id is not None and has_user(proposal.project, user)


def user_can_view_awarded_resources(user, proposal) -> bool:
    """Who may read a proposal's award, beyond being able to see the proposal.

    At any time: staff and support, whoever manages the call (its call
    managers and the organisers of its managing organisation), and the
    managers of the offerings requested or awarded -- they assess the request
    technically and provision the award. The applicant team only once the
    award is released (see ``awarded_resources_released``). Nobody else:
    reviewers and panel members judge the proposal, not the award.

    A call manager or offering manager who also applied waits for the
    release like any applicant: they neither draft nor edit their own award.
    """
    if user.is_staff or user.is_support:
        return True
    if _user_manages_the_call(user, proposal):
        return True
    if not proposal_roles.is_on_applicant_team(
        user, proposal
    ) and _user_manages_an_offering_of(user, proposal):
        return True
    if _user_is_on_the_applicant_team(user, proposal):
        return awarded_resources_released(proposal)
    return False


def can_access_awarded_resources(request, view, obj=None):
    """ActionsPermission check for the awarded resources endpoints.

    Reading follows ``user_can_view_awarded_resources``. Changing the award is
    the call manager's (or staff's); whether it is still open for changes is a
    state question the view answers with 409.
    """
    if obj is None:
        return
    user = request.user
    if request.method in permissions.SAFE_METHODS:
        if not user_can_view_awarded_resources(user, obj):
            raise exceptions.PermissionDenied(
                "The awarded resources are not visible until the decision is released."
            )
        return
    if proposal_roles.is_on_applicant_team(user, obj):
        # Not even as staff: nobody decides their own proposal's award.
        raise exceptions.PermissionDenied(
            "Nobody on the proposal's own team may change its award."
        )
    if has_staff_access(request) or _user_is_call_manager(user, obj):
        return
    raise exceptions.PermissionDenied(
        "Only the call manager can change the awarded resources."
    )


can_access_awarded_resources.sources = ["*"]


def user_is_assignment_reviewer(request, view, obj=None):
    """ActionsPermission check for the assignment accept / decline actions.

    ``AssignmentItem.accept`` files the Review under the batch's reviewer
    whoever calls it, so anyone else accepting would enrol that reviewer into a
    review they never agreed to. Managers intervene through reassign and the
    pool's force_accept instead.
    """
    if obj is None:
        return
    user = request.user
    if has_staff_access(request):
        return
    entry = obj.batch.reviewer_pool_entry
    if entry.reviewer_id and entry.reviewer.user_id == user.id:
        return
    raise exceptions.PermissionDenied(
        "Only the reviewer this assignment belongs to can respond to it."
    )


user_is_assignment_reviewer.sources = ["*"]


def can_manage_call_budgets(request, view, obj=None):
    """The call's budgets and what they have awarded: read by its managers,
    staff and support; written by its managers and staff."""
    if obj is None:
        return
    user = request.user
    if user.is_staff:
        return
    if request.method in permissions.SAFE_METHODS and user.is_support:
        return
    if not has_permission_on_any_source(
        request, PermissionEnum.UPDATE_CALL, obj, CALL_PERMISSION_SOURCES
    ):
        raise exceptions.PermissionDenied()


can_manage_call_budgets.sources = ["*"]


def user_can_view_proposal_budget(user, proposal) -> bool:
    """Whether ``user`` reads where the proposal's award stands against the
    call's budgets: what other proposals were awarded is the call team's,
    never the applicant team's."""
    if user.is_staff or user.is_support:
        return True
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    return oversees_proposal_call(user, proposal) or manages_proposal_workflow(
        user, proposal
    )


def can_view_proposal_budget(request, view, obj=None):
    """Where the proposal's award stands against the call's budgets is read by
    users with the staff or support flag, those overseeing its call and those
    driving its workflow; never by the applicant team
    (``user_can_view_proposal_budget``)."""
    if obj is None:
        return
    if not user_can_view_proposal_budget(request.user, obj):
        raise exceptions.PermissionDenied()


can_view_proposal_budget.sources = ["*"]


# =============================================================================
# Post-award runs: extension requests and the final report
# =============================================================================


def user_can_view_runs(user, proposal) -> bool:
    """Who reads an accepted proposal's extension requests and final report:
    whoever reads its award (``user_can_view_awarded_resources``) -- staff,
    support, the call's managers, the managers of the offerings involved and
    the applicant team once the award is released. Reviewers and panel
    members judged the proposal, not its allocation, and read none of it."""
    return user_can_view_awarded_resources(user, proposal)


def can_view_runs(request, view, obj=None):
    """An accepted proposal's extension requests and final report are read by
    whoever reads its award (``user_can_view_runs``): users with the staff or
    support flag, the call's managers, the managers of the offerings involved,
    and the applicant team once the award is released."""
    if obj is None:
        return
    if not user_can_view_runs(request.user, obj):
        raise exceptions.PermissionDenied(
            "You cannot see this proposal's extension requests and final report."
        )


can_view_runs.sources = ["*"]


def can_request_extension(request, view, obj=None):
    """Only the proposal's team asks for an extension: its managers, who act
    for the applicant (``acts_for_applicant``). Not the members of the
    project granted later, nor staff or the call's managers, who decide it."""
    if obj is None:
        return
    if not acts_for_applicant(request.user, obj):
        raise exceptions.PermissionDenied(
            "Only the proposal's team requests an extension."
        )


can_request_extension.sources = ["*"]


def user_drives_runs(user, proposal) -> bool:
    """Staff, and whoever drives the call's workflow, without being on the
    proposal's team: they open a final report early and call off a run."""
    if user.is_staff:
        return True
    return manages_proposal_workflow(user, proposal)


def can_open_final_report(request, view, obj=None):
    """Staff, and whoever drives the call's workflow without being on the
    proposal's team, open a final report ahead of time
    (``user_drives_runs``)."""
    if obj is None:
        return
    if not user_drives_runs(request.user, obj):
        raise exceptions.PermissionDenied(
            "Only the call's managers open a final report ahead of time."
        )


can_open_final_report.sources = ["*"]


def can_provision_awarded_item(request, view, obj=None):
    """Staff, and whoever drives the call's workflow without being on the
    proposal's team, order an awarded item again (``user_drives_runs``)."""
    if obj is None:
        return
    if not user_drives_runs(request.user, obj):
        raise exceptions.PermissionDenied(
            "Only the call's managers order an awarded item again."
        )


can_provision_awarded_item.sources = ["*"]


def reads_proposal_checklist(request_or_user, proposal) -> bool:
    """Whether ``user`` reads ``proposal``'s compliance answers -- and so the
    files attached to them.

    Staff and support; the applicant team, and whoever may edit the proposal;
    whoever lists the call's proposals, on the call or its managing
    organisation; the call's managers; a reviewer holding a live review of
    it; and the holder of an evaluator role on it. The answers' endpoint and
    the download of their files read this one rule, so a file never stays
    closed to someone who reads the answer it belongs to.
    """
    # A request carries the token's scopes, which has_permission honours.
    user = getattr(request_or_user, "user", request_or_user)
    if user is None or user.is_anonymous:
        return False
    # A token without the staff or support scope does not act as either.
    if (
        user.is_staff or user.is_support
        if user is request_or_user
        else has_support_access(request_or_user)
    ):
        return True
    call = proposal.round.call
    return (
        proposal_roles.is_on_applicant_team(user, proposal)
        or has_permission(request_or_user, PermissionEnum.UPDATE_PROPOSAL, proposal)
        or has_permission(request_or_user, PermissionEnum.LIST_PROPOSALS, call)
        or has_permission(request_or_user, PermissionEnum.LIST_PROPOSALS, call.manager)
        or call.pk in managers.get_connected_calls(user, RoleEnum.CALL_MANAGER)
        or managers.holds_live_review(user, proposal)
        or proposal_roles.holds_role(user, proposal)
    )
