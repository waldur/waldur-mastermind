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

from django.db.models import Q

from waldur_core.configuration import config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers,
    get_connected_customers_by_permission,
    get_connected_projects,
)
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat import hooks as chat_hooks
from waldur_mastermind.chat.tools.account.helpers import (
    user_accessible_projects,
    user_consumer_resources,
)
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user
from waldur_mastermind.invoices.models import (
    CustomerCredit,
    InvoiceItem,
    ProjectCredit,
)
from waldur_mastermind.marketplace.enums import BillingTypes, OrderStates
from waldur_mastermind.marketplace.managers import filter_orders_for_user
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


def _own(user, queryset, project="project", customer="project__customer"):
    """Narrow ``queryset`` to the caller's own projects and organizations
    when the caller is staff or support, who otherwise see the whole
    deployment: "your paused resource" must be theirs, not anybody's."""
    if not (user.is_staff or user.is_support):
        return queryset
    return queryset.filter(
        Q(**{f"{project}__in": get_connected_projects(user)})
        | Q(**{f"{customer}__in": get_connected_customers(user)})
    )


def _live_resources(user):
    return _own(
        user,
        user_consumer_resources(user).exclude(state=Resource.States.TERMINATED),
    )


def has_paused_resource(user) -> bool:
    return _live_resources(user).filter(paused=True).exists()


def has_review_work(user) -> bool:
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


# Every state in which an order waits on someone, as list_orders' "pending".
_PENDING_ORDER_STATES = (
    OrderStates.PENDING_CONSUMER,
    OrderStates.PENDING_PROVIDER,
    OrderStates.PENDING_PROJECT,
    OrderStates.PENDING_START_DATE,
)


def _has_pending_order(user) -> bool:
    # The orders list_orders shows the caller, so also those waiting on the
    # caller's approval as a project manager, owner or provider.
    orders = filter_orders_for_user(Order.objects.all(), user)
    return _own(user, orders).filter(state__in=_PENDING_ORDER_STATES).exists()


def _has_credit(user) -> bool:
    customer_credits = filter_queryset_for_user(CustomerCredit.objects.all(), user)
    project_credits = filter_queryset_for_user(ProjectCredit.objects.all(), user)
    return (
        _own(
            user, customer_credits, project="customer__projects", customer="customer"
        ).exists()
        or _own(user, project_credits).exists()
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


def has_hpc_allocation(user) -> bool:
    resources = chat_hooks.get_hpc_resources_for_user(user)
    if resources is None:
        return False
    return _own(user, resources).exists()


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


def _sees_costs(user) -> bool:
    """Costs of an organization or project the caller may chart: the invoice
    items the REST costs endpoint would show them."""
    items = filter_queryset_for_user(InvoiceItem.objects.all(), user)
    return _own(user, items, customer="invoice__customer").exists()


def _staff_with_credits(user) -> bool:
    return (user.is_staff or user.is_support) and ProjectCredit.objects.exists()


_PRICES_SHOWN = "!feature:marketplace.conceal_prices"
# A catalogue-only portal lists offerings but takes no orders.
_ORDERING = "!feature:marketplace.catalogue_only"
# A call-only portal runs proposals and reviews outside Waldur.
_CALLS_IN_WALDUR = "!feature:marketplace.call_only"

# In the order they are offered: the user's own state first (something needs
# their attention), then what the portal offers.
CATALOG: tuple[Suggestion, ...] = (
    Suggestion(
        "resource_paused",
        (ToolName.EXPLAIN_RESOURCE_PAUSED_REASON,),
        check=has_paused_resource,
    ),
    Suggestion(
        "review_workload",
        (ToolName.REVIEW_WORKLOAD,),
        requires=(_CALLS_IN_WALDUR,),
        check=has_review_work,
    ),
    Suggestion(
        "proposal_help",
        (ToolName.GUIDE_PROPOSAL, ToolName.PROPOSAL_OVERVIEW),
        requires=(_CALLS_IN_WALDUR,),
        check=_has_draft_proposal,
    ),
    Suggestion(
        "pending_orders",
        (ToolName.LIST_ORDERS,),
        requires=(_ORDERING,),
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
        requires=("offering_type:OpenStack.Instance", _ORDERING),
        check=_has_project,
    ),
    Suggestion(
        "hpc_jobs",
        (ToolName.LIST_HPC_JOBS, ToolName.PLAN_HPC_JOB),
        requires=("capability:hpc",),
        check=has_hpc_allocation,
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
        "visualize_costs",
        (ToolName.CHART_COSTS,),
        requires=(_PRICES_SHOWN,),
        check=_sees_costs,
    ),
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
        check=has_hpc_allocation,
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
