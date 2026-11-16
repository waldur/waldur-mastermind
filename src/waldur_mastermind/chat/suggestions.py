"""Quick prompts the AI assistant offers on an empty thread.

Each entry names the tools it needs, the deployment configuration it depends
on (as ``deployment_profile`` terms, the vocabulary the documentation uses)
and, optionally, a check on the user's own data. An entry is offered only when
all three pass, so a quick prompt can never lead to a tool the user lacks or a
feature the portal has switched off. Homeport owns the wording and icons and
looks them up by ``id``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from waldur_core.configuration import config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers_by_permission,
)
from waldur_firecrest import workspace
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat.tools.account.helpers import (
    user_accessible_projects,
    user_consumer_resources,
)
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user
from waldur_mastermind.invoices.models import CustomerCredit, ProjectCredit
from waldur_mastermind.marketplace.enums import BillingTypes, OrderStates
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ReviewerPoolInvitationStatuses,
    ReviewStates,
)
from waldur_mastermind.proposal.models import (
    AssignmentItem,
    Call,
    Proposal,
    Review,
    ReviewerProfile,
)


@dataclass(frozen=True)
class Suggestion:
    id: str
    tools: tuple[ToolName, ...]
    requires: tuple[str, ...] = ()
    check: Callable | None = None


def _live_resources(user):
    return user_consumer_resources(user).exclude(state=Resource.States.TERMINATED)


def _has_paused_resource(user) -> bool:
    return _live_resources(user).filter(paused=True).exists()


def _has_review_work(user) -> bool:
    if config.REVIEWER_PROFILES_ENABLED and (
        ReviewerProfile.objects.filter(user=user).exists()
    ):
        return True
    if Review.objects.filter(reviewer=user, state=ReviewStates.IN_REVIEW).exists():
        return True
    return AssignmentItem.objects.filter(
        batch__reviewer_pool_entry__reviewer__user=user,
        batch__reviewer_pool_entry__invitation_status=(
            ReviewerPoolInvitationStatuses.ACCEPTED
        ),
        batch__status__in=AssignmentBatchStatuses.SENT_TO_REVIEWER,
        status=AssignmentItemStatuses.PENDING,
    ).exists()


def _has_draft_proposal(user) -> bool:
    return Proposal.objects.filter(
        created_by=user, state=Proposal.States.DRAFT
    ).exists()


def _has_pending_order(user) -> bool:
    return Order.objects.filter(
        created_by=user,
        state__in=(
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.PENDING_PROJECT,
        ),
    ).exists()


def _has_credit(user) -> bool:
    return (
        filter_queryset_for_user(CustomerCredit.objects.all(), user).exists()
        or filter_queryset_for_user(ProjectCredit.objects.all(), user).exists()
    )


def _has_resource(user) -> bool:
    return _live_resources(user).exists()


def _has_usage_resource(user) -> bool:
    return (
        _live_resources(user)
        .filter(offering__components__billing_type=BillingTypes.USAGE)
        .exists()
    )


def _has_project(user) -> bool:
    return user_accessible_projects(user).exists()


def _has_hpc_allocation(user) -> bool:
    return workspace.resources_for_user(user).exists()


_MARKETPLACE_HIDDEN = "feature:marketplace.hide_marketplace_from_end_users"


def _sees_marketplace(user) -> bool:
    if user.is_staff or user.is_support:
        return True
    state = deployment_profile.get_term_states([_MARKETPLACE_HIDDEN])
    return state[_MARKETPLACE_HIDDEN] != deployment_profile.TermState.ON


def _has_open_call(user) -> bool:
    return Call.objects.filter(state=Call.States.ACTIVE).exists()


def _has_call(user) -> bool:
    return Call.objects.exists()


def _reads_invoices(user) -> bool:
    return (
        user.is_staff
        or user.is_support
        or get_connected_customers_by_permission(
            user, PermissionEnum.LIST_INVOICES
        ).exists()
    )


def _staff_with_credits(user) -> bool:
    return (user.is_staff or user.is_support) and ProjectCredit.objects.exists()


_PRICES_SHOWN = "!feature:marketplace.conceal_prices"

# In the order they are offered: the user's own state first (something needs
# their attention), then what the portal offers.
CATALOG: tuple[Suggestion, ...] = (
    Suggestion(
        "resource_paused",
        (ToolName.EXPLAIN_RESOURCE_PAUSED_REASON,),
        check=_has_paused_resource,
    ),
    Suggestion(
        "review_workload",
        (ToolName.REVIEW_WORKLOAD,),
        check=_has_review_work,
    ),
    Suggestion(
        "proposal_help",
        (ToolName.GUIDE_PROPOSAL, ToolName.PROPOSAL_OVERVIEW),
        requires=("!feature:marketplace.call_only",),
        check=_has_draft_proposal,
    ),
    Suggestion(
        "pending_orders",
        (ToolName.LIST_ORDERS,),
        check=_has_pending_order,
    ),
    Suggestion(
        "check_credit",
        (
            ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE,
            ToolName.GET_CUSTOMER_CREDIT_OVERVIEW,
        ),
        check=_has_credit,
    ),
    Suggestion(
        "my_resources",
        (ToolName.GET_PROJECT_RESOURCES, ToolName.DISPLAY_USER_RESOURCES),
        check=_has_resource,
    ),
    Suggestion(
        "analyze_usage",
        (ToolName.GET_RESOURCE_USAGE,),
        check=_has_usage_resource,
    ),
    Suggestion(
        "create_vm",
        (ToolName.PLAN_VM, ToolName.CREATE_VM),
        requires=("offering_type:OpenStack.Instance",),
        check=_has_project,
    ),
    Suggestion(
        "hpc_jobs",
        (ToolName.LIST_HPC_JOBS, ToolName.PLAN_HPC_JOB),
        requires=("capability:hpc",),
        check=_has_hpc_allocation,
    ),
    Suggestion(
        "find_service",
        (ToolName.SEARCH_OFFERINGS, ToolName.LIST_CATEGORIES),
        check=_sees_marketplace,
    ),
    Suggestion(
        "find_call",
        (ToolName.FIND_MATCHING_CALLS,),
        check=_has_open_call,
    ),
    Suggestion("search_docs", (ToolName.SEARCH_DOCS,)),
    Suggestion(
        "list_invoices",
        (ToolName.LIST_INVOICES,),
        requires=(_PRICES_SHOWN,),
        check=_reads_invoices,
    ),
    Suggestion(
        "hpc_software",
        (ToolName.FIND_HPC_SOFTWARE,),
        requires=("capability:hpc", "feature:marketplace.display_software_catalog"),
        check=_has_hpc_allocation,
    ),
    Suggestion(
        "call_insights",
        (ToolName.CALL_INSIGHTS,),
        requires=("feature:marketplace.show_call_management_functionality",),
        check=_has_call,
    ),
    Suggestion(
        "overdrawn_projects",
        (ToolName.LIST_OVERDRAWN_PROJECTS,),
        check=_staff_with_credits,
    ),
    Suggestion("look_up_user", (ToolName.GET_USER_OVERVIEW,)),
)

SUGGESTION_IDS = tuple(s.id for s in CATALOG)


def suggestions_for(user, delegated: bool = False) -> list[str]:
    """Ids of the catalog entries this user is offered, in catalog order.

    The tool set is the one the stream uses for the same request, so a
    delegated session is not offered what it cannot run. Configuration terms
    come from the cached deployment profile; each remaining entry costs at
    most a query or two of its own, however many organizations the user has.
    """
    permitted = set(get_tool_set_for_user(user, delegated))
    states = deployment_profile.get_term_states(
        term for entry in CATALOG for term in entry.requires
    )
    offered = []
    for entry in CATALOG:
        if not set(entry.tools) <= permitted:
            continue
        if any(states[t] != deployment_profile.TermState.ON for t in entry.requires):
            continue
        if entry.check and not entry.check(user):
            continue
        offered.append(entry.id)
    return offered
