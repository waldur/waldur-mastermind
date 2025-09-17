import logging
import re

from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import get_system_robot
from waldur_core.logging import event_logger
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
from waldur_mastermind.policy import models, tasks

from . import enums, structures

logger = logging.getLogger(__name__)


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
        event_context={"policy_uuid": policy.uuid.hex},
        scopes=[],
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
        event_context={"policy_uuid": policy.uuid.hex},
        scopes=[],
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
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

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
                event_context={"policy_uuid": policy.uuid.hex},
                scopes=[],
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
            event_context={"policy_uuid": policy.uuid.hex},
            scopes=[],
        )
        raise PolicyException(
            f"Creation of new resources in this project is prohibited by policy {policy.uuid.hex}."
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
            event_context={"policy_uuid": policy.uuid.hex},
            scopes=[],
        )
        raise PolicyException(
            "Modification of new resources in this project is not available due to a policy."
        )


def request_downscaling(policy: models.Policy):
    resources = marketplace_models.Resource.objects.filter(
        offering__plugin_options__supports_downscaling=True
    ).exclude(
        state__in=(
            ResourceStates.TERMINATED,
            ResourceStates.TERMINATING,
        )
    )

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

    resources.update(downscaled=True)
    logger.info(
        "Policy action request_downscaling has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )
    event_logger.emit(
        "Cost policy has been triggered and downscaling has been requested. Resources: %s"
        % ", ".join([str(r) for r in resources]),
        event_type=EventType.BLOCK_MODIFICATION_OF_EXISTING_RESOURCES,
        event_context={"policy_uuid": policy.uuid.hex},
        scopes=[],
    )


def reset_downscaling(policy: models.Policy):
    resources = marketplace_models.Resource.objects.filter(
        offering__plugin_options__supports_downscaling=True
    ).exclude(
        state__in=(
            ResourceStates.TERMINATED,
            ResourceStates.TERMINATING,
        )
    )

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

    resources.update(downscaled=False)
    logger.info(
        "Policy action reset_downscaling has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )


def restrict_members(policy: models.Policy):
    resources = marketplace_models.Resource.objects.filter(
        offering__plugin_options__service_provider_can_create_offering_user=True
    ).exclude(state__in=(ResourceStates.TERMINATED,))

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

    resources.update(restrict_member_access=True)

    logger.info(
        "Policy action restrict_members has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )
    event_logger.emit(
        "Cost policy has been triggered and account removal has been requested. Resources: %s"
        % ", ".join([str(r) for r in resources]),
        event_type=EventType.RESTRICT_MEMBERS,
        event_context={"policy_uuid": policy.uuid.hex},
        scopes=[],
    )


def reset_member_restriction(policy: models.Policy):
    resources = marketplace_models.Resource.objects.filter(
        offering__plugin_options__service_provider_can_create_offering_user=True
    ).exclude(state__in=(ResourceStates.TERMINATED,))

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

    resources.update(restrict_member_access=False)

    logger.info(
        "Policy action restrict_members has been reset. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([f"{r.name} ({r.slug})" for r in resources]),
    )


def request_pausing(policy: models.Policy):
    resources = marketplace_models.Resource.objects.filter(
        offering__plugin_options__supports_pausing=True
    ).exclude(state__in=(ResourceStates.TERMINATED,))

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

    resources.update(paused=True)
    logger.info(
        "Policy action request_pausing has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )
    event_logger.emit(
        "Cost policy has been triggered and pausing has been requested. Resources: %s"
        % ", ".join([str(r) for r in resources]),
        event_type=EventType.BLOCK_MODIFICATION_OF_EXISTING_RESOURCES,
        event_context={"policy_uuid": policy.uuid.hex},
        scopes=[],
    )


def reset_pausing(policy: models.Policy):
    resources = marketplace_models.Resource.objects.filter(
        offering__plugin_options__supports_pausing=True
    ).exclude(state__in=(ResourceStates.TERMINATED,))

    if isinstance(policy.scope, Project):
        resources = resources.filter(project=policy.scope)
    elif isinstance(policy.scope, Customer):
        resources = resources.filter(project__customer=policy.scope)
    else:
        # Assuming that policy scope is an offering
        resources = resources.filter(offeirng=policy.scope)

    resources.update(paused=False)
    logger.info(
        "Policy action reset_pausing has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
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
        event_context={"policy_uuid": policy.uuid.hex},
        scopes=[],
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
}
