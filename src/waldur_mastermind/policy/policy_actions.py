import logging
import re

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from reversion import revisions as reversion

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import get_system_robot
from waldur_core.logging import event_logger
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.marketplace.models import Offering
from waldur_mastermind.policy import models, tasks

from . import enums, structures

logger = logging.getLogger(__name__)


def _get_cost_policy_context(policy):
    """Build extra event context with cost policy state for debugging.

    Returns a dict with limit_cost and credit balance fields when available.
    Only adds fields that are cheap to compute (no extra queries beyond
    the credit lookup).
    """
    from waldur_mastermind.invoices.models import CustomerCredit, ProjectCredit

    ctx = {}

    if not hasattr(policy, "limit_cost"):
        return ctx

    ctx["limit_cost"] = policy.limit_cost

    scope = policy.scope
    if isinstance(scope, Project):
        project_credit = ProjectCredit.objects.filter(project=scope).first()
        if project_credit:
            ctx["project_credit_balance"] = int(project_credit.value)
        customer_credit = CustomerCredit.objects.filter(customer=scope.customer).first()
        if customer_credit:
            ctx["credit_balance"] = int(customer_credit.value)
    elif isinstance(scope, Customer):
        customer_credit = CustomerCredit.objects.filter(customer=scope).first()
        if customer_credit:
            ctx["credit_balance"] = int(customer_credit.value)

    return ctx


def _get_base_policy_scopes(policy):
    """Return policy-level scope objects (without resource). Compute once per action."""
    scope = policy.scope
    if isinstance(scope, Project):
        return [scope, scope.customer]
    elif isinstance(scope, Customer):
        return [scope]
    elif isinstance(scope, Offering):
        return [scope, scope.customer]
    return []


def _save_resource_with_reversion(
    resource,
    policy,
    action_name,
    field_name,
    new_value,
    extra_comment="",
    system_robot=None,
):
    """Save a resource field change with reversion tracking and policy attribution.

    Caller should pass system_robot to avoid repeated get_system_robot() lookups.
    """
    old_value = getattr(resource, field_name)
    setattr(resource, field_name, new_value)

    scope_name = str(policy.scope) if policy.scope else ""

    # Collect policy details for audit trail (varies by policy type)
    policy_details = {}
    if hasattr(policy, "limit_cost"):
        policy_details["limit_cost"] = str(policy.limit_cost)
    if hasattr(policy, "limit_type"):
        policy_details["limit_type"] = policy.limit_type
    if hasattr(policy, "grace_ratio"):
        policy_details["grace_ratio"] = str(policy.grace_ratio)
    if hasattr(policy, "actions") and isinstance(policy.actions, str):
        policy_details["actions"] = policy.actions

    comment = (
        f"Policy action '{action_name}': {field_name} changed from {old_value} to {new_value}. "
        f"Policy: {type(policy).__name__} {policy.uuid.hex}. "
        f"Scope: {scope_name}."
    )
    if policy_details.get("limit_cost"):
        comment += f" Threshold: {policy_details['limit_cost']}."
    if extra_comment:
        comment += f" {extra_comment}"

    # Store attribution metadata on the resource for frontend tooltips
    if not resource.attributes:
        resource.attributes = {}
    attribution = resource.attributes.get("_policy_attribution", {})
    attribution[field_name] = {
        "policy_class": type(policy).__name__,
        "policy_uuid": policy.uuid.hex,
        "action": action_name,
        "scope_name": scope_name,
        "timestamp": timezone.now().isoformat(),
        **policy_details,
    }
    resource.attributes["_policy_attribution"] = attribution

    if system_robot is None:
        system_robot = get_system_robot()

    # Mark as mocked to prevent re-entrant policy evaluation: the resource.save()
    # triggers post_save → policy handler, which would evaluate other policies on the
    # same project and potentially raise PolicyException, crashing the current action.
    resource.is_mocked = True
    try:
        with reversion.create_revision():
            resource.save(update_fields=[field_name, "attributes"])
            reversion.set_user(system_robot)
            reversion.set_comment(comment)
    finally:
        resource.is_mocked = False


def _emit_events_bulk(pending_events):
    """Bulk-create Event and Feed records collected during a policy action loop.

    pending_events: list of dicts with keys: message, event_type, event_context, scopes

    Reduces N individual Event INSERTs + 3N individual Feed INSERTs to
    2 bulk INSERTs regardless of how many resources were affected.
    """
    if not pending_events:
        return

    events = logging_models.Event.objects.bulk_create(
        [
            logging_models.Event(
                event_type=pe["event_type"],
                message=pe["message"],
                context=event_logger.compile_context(**pe["event_context"]),
            )
            for pe in pending_events
        ]
    )

    # ContentType.objects.get_for_model() has an internal cache, so repeated
    # calls for the same model class are cheap (dict lookup, no DB query).
    feeds = []
    for event, pe in zip(events, pending_events):
        for scope in pe.get("scopes") or []:
            if scope and scope.id:
                ct = ContentType.objects.get_for_model(scope)
                feeds.append(
                    logging_models.Feed(
                        event=event,
                        content_type=ct,
                        object_id=scope.id,
                    )
                )
    if feeds:
        logging_models.Feed.objects.bulk_create(feeds)


def request_slurm_resource_downscaling(policy: models.Policy):
    """SLURM-specific downscaling for individual resource management."""

    if not hasattr(policy, "get_resource_usage_percentage"):
        request_downscaling(policy)
        return

    resources = marketplace_models.Resource.objects.filter(
        offering=policy.scope, offering__plugin_options__supports_downscaling=True
    ).exclude(state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING))

    base_scopes = _get_base_policy_scopes(policy)
    system_robot = get_system_robot()
    pending_events = []

    with transaction.atomic():
        for resource in resources:
            usage_percentage = policy.get_resource_usage_percentage(resource)

            if usage_percentage >= 100 and not resource.downscaled:
                _save_resource_with_reversion(
                    resource,
                    policy,
                    "request_slurm_resource_downscaling",
                    "downscaled",
                    True,
                    extra_comment=f"Usage: {usage_percentage:.1f}%.",
                    system_robot=system_robot,
                )
                logger.info(
                    f"SLURM resource {resource.uuid} downscaled: {usage_percentage:.1f}% usage"
                )
                pending_events.append(
                    {
                        "message": f"SLURM usage policy has triggered downscaling of resource {resource.name}. Usage: {usage_percentage:.1f}%.",
                        "event_type": EventType.REQUEST_SLURM_RESOURCE_DOWNSCALING,
                        "event_context": {"policy_uuid": policy.uuid.hex},
                        "scopes": [resource] + base_scopes,
                    }
                )
            elif resource.downscaled and usage_percentage < 100:
                _save_resource_with_reversion(
                    resource,
                    policy,
                    "request_slurm_resource_downscaling",
                    "downscaled",
                    False,
                    extra_comment=f"Usage: {usage_percentage:.1f}%.",
                    system_robot=system_robot,
                )
                logger.info(
                    f"SLURM resource {resource.uuid} downscaling removed: {usage_percentage:.1f}% usage"
                )
                pending_events.append(
                    {
                        "message": f"SLURM usage policy has removed downscaling of resource {resource.name}. Usage: {usage_percentage:.1f}%.",
                        "event_type": EventType.RESET_DOWNSCALING,
                        "event_context": {"policy_uuid": policy.uuid.hex},
                        "scopes": [resource] + base_scopes,
                    }
                )

    _emit_events_bulk(pending_events)


def request_slurm_resource_pausing(policy: models.Policy):
    """SLURM-specific pausing for individual resource management."""

    if not (
        hasattr(policy, "get_resource_usage_percentage")
        and hasattr(policy, "grace_ratio")
    ):
        request_pausing(policy)
        return

    resources = marketplace_models.Resource.objects.filter(
        offering=policy.scope, offering__plugin_options__supports_pausing=True
    ).exclude(state__in=(ResourceStates.TERMINATED,))

    grace_limit = (1 + policy.grace_ratio) * 100
    base_scopes = _get_base_policy_scopes(policy)
    system_robot = get_system_robot()
    pending_events = []

    with transaction.atomic():
        for resource in resources:
            usage_percentage = policy.get_resource_usage_percentage(resource)

            if usage_percentage >= grace_limit and not resource.paused:
                _save_resource_with_reversion(
                    resource,
                    policy,
                    "request_slurm_resource_pausing",
                    "paused",
                    True,
                    extra_comment=f"Usage: {usage_percentage:.1f}%, grace limit: {grace_limit:.1f}%.",
                    system_robot=system_robot,
                )
                logger.info(
                    f"SLURM resource {resource.uuid} paused: {usage_percentage:.1f}% usage (grace limit: {grace_limit:.1f}%)"
                )
                pending_events.append(
                    {
                        "message": f"SLURM usage policy has triggered pausing of resource {resource.name}. Usage: {usage_percentage:.1f}%, grace limit: {grace_limit:.1f}%.",
                        "event_type": EventType.REQUEST_SLURM_RESOURCE_PAUSING,
                        "event_context": {"policy_uuid": policy.uuid.hex},
                        "scopes": [resource] + base_scopes,
                    }
                )
            elif resource.paused and usage_percentage < grace_limit:
                _save_resource_with_reversion(
                    resource,
                    policy,
                    "request_slurm_resource_pausing",
                    "paused",
                    False,
                    extra_comment=f"Usage: {usage_percentage:.1f}%, grace limit: {grace_limit:.1f}%.",
                    system_robot=system_robot,
                )
                logger.info(
                    f"SLURM resource {resource.uuid} pausing removed: {usage_percentage:.1f}% usage"
                )
                pending_events.append(
                    {
                        "message": f"SLURM usage policy has removed pausing of resource {resource.name}. Usage: {usage_percentage:.1f}%.",
                        "event_type": EventType.RESET_PAUSING,
                        "event_context": {"policy_uuid": policy.uuid.hex},
                        "scopes": [resource] + base_scopes,
                    }
                )

    _emit_events_bulk(pending_events)


def notify_project_team(policy: models.Policy):
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_project_team.delay(serialized_policy)

    logger.info(
        "Policy action notify_project_team has been triggered. Policy UUID: %s.",
        policy.uuid.hex,
    )

    event_logger.emit(
        "Cost policy has been triggered and notification to project members has been scheduled.",
        event_type=EventType.NOTIFY_PROJECT_TEAM,
        event_context={
            "policy_uuid": policy.uuid.hex,
            **_get_cost_policy_context(policy),
        },
        scopes=_get_base_policy_scopes(policy),
    )


def notify_organization_owners(policy: models.Policy):
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_customer_owners.delay(serialized_policy)

    logger.info(
        "Policy action notify_organization_owners has been triggered. Policy UUID: %s.",
        policy.uuid.hex,
    )

    event_logger.emit(
        "Cost policy has been triggered and notification to organization owners has been scheduled.",
        event_type=EventType.NOTIFY_ORGANIZATION_OWNERS,
        event_context={
            "policy_uuid": policy.uuid.hex,
            **_get_cost_policy_context(policy),
        },
        scopes=_get_base_policy_scopes(policy),
    )


def terminate_resources(policy: models.Policy):
    from waldur_mastermind.marketplace import tasks as marketplace_tasks

    user = get_system_robot()

    resources = marketplace_models.Resource.objects.exclude(
        state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING)
    )

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    elif isinstance(policy.scope, Offering):
        resources = resources.filter(offering=policy.scope)
    else:
        logger.error(f"Unsupported policy scope type: {type(policy.scope)}")
        return

    for resource in resources:
        with transaction.atomic():
            attributes = (
                {"action": "force_destroy"}
                if resource.offering.type == OPENSTACK_INSTANCE_OFFERING
                else {}
            )
            order = marketplace_models.Order.objects.create(
                resource=resource,
                offering=resource.offering,
                type=OrderTypes.TERMINATE,
                state=OrderStates.EXECUTING,
                attributes=attributes,
                project=resource.project,
                created_by=user,
                consumer_reviewed_by=user,
            )

            logger.info(
                "Policy created termination order. Policy UUID: %s. Resource: %s",
                policy.uuid.hex,
                str(resource),
            )

            event_logger.emit(
                "Cost policy has been triggered and termination order has been created. Resource: %s."
                % str(resource),
                event_type=EventType.TERMINATE_RESOURCES,
                event_context={
                    "policy_uuid": policy.uuid.hex,
                    **_get_cost_policy_context(policy),
                },
                scopes=[resource] + _get_base_policy_scopes(policy),
            )

            marketplace_tasks.process_order_on_commit(order, user)


def block_creation_of_new_resources(policy, created):
    if created:
        logger.info(
            "Policy action block_creation_of_new_resources has been triggered. Policy UUID: %s.",
            policy.uuid.hex,
        )
        event_logger.emit(
            "Cost policy has been triggered and creation of new resource has been blocked.",
            event_type=EventType.BLOCK_CREATION_OF_NEW_RESOURCES,
            event_context={
                "policy_uuid": policy.uuid.hex,
                **_get_cost_policy_context(policy),
            },
            scopes=_get_base_policy_scopes(policy),
        )
        raise PolicyException(
            f"Creation of new resources in this project is prohibited by {policy}."
        )


def block_modification_of_existing_resources(policy, created):
    if not created:
        logger.info(
            "Policy action block_modification_of_existing_resources has been triggered. Policy UUID: %s.",
            policy.uuid.hex,
        )
        event_logger.emit(
            "Cost policy has been triggered and updating existing resource has been blocked.",
            event_type=EventType.BLOCK_MODIFICATION_OF_EXISTING_RESOURCES,
            event_context={
                "policy_uuid": policy.uuid.hex,
                **_get_cost_policy_context(policy),
            },
            scopes=_get_base_policy_scopes(policy),
        )
        raise PolicyException(
            f"Modification of new resources in this project is not available due to a {policy}."
        )


def _filter_resources_by_scope(resources, policy):
    """Filter a resource queryset by the policy's scope type."""
    if isinstance(policy.scope, Project):
        return resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        return resources.filter(project__customer=policy.scope)
    elif isinstance(policy.scope, Offering):
        return resources.filter(offering=policy.scope)
    else:
        logger.error(f"Unsupported policy scope type: {type(policy.scope)}")
        return None


def _apply_generic_action(
    policy, action_name, field_name, new_value, event_type, queryset
):
    """Shared implementation for the 6 generic resource-modifying policy actions.

    Optimised for high resource counts:
    - Pre-computes policy scopes and system_robot once
    - Wraps all resource saves in a single transaction
    - Bulk-creates Event and Feed records after the loop
    """
    resources = _filter_resources_by_scope(queryset, policy)
    if resources is None:
        return

    base_scopes = _get_base_policy_scopes(policy)
    system_robot = get_system_robot()
    cost_policy_ctx = _get_cost_policy_context(policy)
    resource_names = []
    pending_events = []

    with transaction.atomic():
        for resource in resources:
            current_value = getattr(resource, field_name)
            if current_value == new_value:
                continue

            _save_resource_with_reversion(
                resource,
                policy,
                action_name,
                field_name,
                new_value,
                system_robot=system_robot,
            )
            resource_names.append(resource.name)
            pending_events.append(
                {
                    "message": f"Cost policy has triggered {action_name} on resource {resource.name}.",
                    "event_type": event_type,
                    "event_context": {
                        "policy_uuid": policy.uuid.hex,
                        **cost_policy_ctx,
                    },
                    "scopes": [resource] + base_scopes,
                }
            )

    _emit_events_bulk(pending_events)

    logger.info(
        "Policy action %s has been triggered. Policy UUID: %s. Resources: %s",
        action_name,
        policy.uuid.hex,
        ", ".join(resource_names),
    )


def request_downscaling(policy: models.Policy):
    _apply_generic_action(
        policy,
        action_name="request_downscaling",
        field_name="downscaled",
        new_value=True,
        event_type=EventType.REQUEST_DOWNSCALING,
        queryset=marketplace_models.Resource.objects.filter(
            offering__plugin_options__supports_downscaling=True
        ).exclude(state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING)),
    )


def reset_downscaling(policy: models.Policy):
    _apply_generic_action(
        policy,
        action_name="reset_downscaling",
        field_name="downscaled",
        new_value=False,
        event_type=EventType.RESET_DOWNSCALING,
        queryset=marketplace_models.Resource.objects.filter(
            offering__plugin_options__supports_downscaling=True
        ).exclude(state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING)),
    )


def restrict_members(policy: models.Policy):
    _apply_generic_action(
        policy,
        action_name="restrict_members",
        field_name="restrict_member_access",
        new_value=True,
        event_type=EventType.RESTRICT_MEMBERS,
        queryset=marketplace_models.Resource.objects.filter(
            offering__plugin_options__service_provider_can_create_offering_user=True
        ).exclude(state__in=(ResourceStates.TERMINATED,)),
    )


def reset_member_restriction(policy: models.Policy):
    _apply_generic_action(
        policy,
        action_name="reset_member_restriction",
        field_name="restrict_member_access",
        new_value=False,
        event_type=EventType.RESET_MEMBER_RESTRICTION,
        queryset=marketplace_models.Resource.objects.filter(
            offering__plugin_options__service_provider_can_create_offering_user=True
        ).exclude(state__in=(ResourceStates.TERMINATED,)),
    )


def request_pausing(policy: models.Policy):
    _apply_generic_action(
        policy,
        action_name="request_pausing",
        field_name="paused",
        new_value=True,
        event_type=EventType.REQUEST_PAUSING,
        queryset=marketplace_models.Resource.objects.filter(
            offering__plugin_options__supports_pausing=True
        ).exclude(state__in=(ResourceStates.TERMINATED,)),
    )


def reset_pausing(policy: models.Policy):
    _apply_generic_action(
        policy,
        action_name="reset_pausing",
        field_name="paused",
        new_value=False,
        event_type=EventType.RESET_PAUSING,
        queryset=marketplace_models.Resource.objects.filter(
            offering__plugin_options__supports_pausing=True
        ).exclude(state__in=(ResourceStates.TERMINATED,)),
    )


def notify_external_user(policy: models.Policy):
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_external_user.delay(serialized_policy)

    logger.info(
        "Policy action notify_external_user has been triggered. Policy UUID: %s.",
        policy.uuid.hex,
    )

    event_logger.emit(
        "Cost policy has been triggered and notification to external user has been scheduled.",
        event_type=EventType.NOTIFY_EXTERNAL_USER,
        event_context={
            "policy_uuid": policy.uuid.hex,
            **_get_cost_policy_context(policy),
        },
        scopes=_get_base_policy_scopes(policy),
    )


def notify_external_user_validator(input_value):
    if not isinstance(input_value, str):
        return False

    email_regex = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    emails = input_value.split(",")

    for email in emails:
        email = email.strip()
        if email and not email_regex.match(email):
            return False

    return True


POLICY_ACTIONS = {
    "notify_project_team": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=notify_project_team,
        reset_method=None,
    ),
    "notify_organization_owners": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=notify_organization_owners,
        reset_method=None,
    ),
    "terminate_resources": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=terminate_resources,
        reset_method=None,
    ),
    "block_creation_of_new_resources": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.THRESHOLD,
        method=block_creation_of_new_resources,
        reset_method=None,
    ),
    "block_modification_of_existing_resources": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.THRESHOLD,
        method=block_modification_of_existing_resources,
        reset_method=None,
        ignored_fields=["modified", "current_usages", "report", "last_sync"],
    ),
    "request_downscaling": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=request_downscaling,
        reset_method=reset_downscaling,
    ),
    "restrict_members": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=restrict_members,
        reset_method=reset_member_restriction,
    ),
    "request_pausing": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=request_pausing,
        reset_method=reset_pausing,
    ),
    "notify_external_user": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=notify_external_user,
        reset_method=None,
        options_validator=notify_external_user_validator,
    ),
    "request_slurm_resource_downscaling": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=request_slurm_resource_downscaling,
        reset_method=None,  # Recovery is built into the method
    ),
    "request_slurm_resource_pausing": structures.PolicyAction(
        action_type=enums.PolicyActionTypes.IMMEDIATE,
        method=request_slurm_resource_pausing,
        reset_method=None,  # Recovery is built into the method
    ),
}
