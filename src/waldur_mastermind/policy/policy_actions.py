import logging

from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import get_system_robot
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.exceptions import PolicyException
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_mastermind.policy import log, tasks

from . import enums, structures

logger = logging.getLogger(__name__)


def notify_project_team(policy):
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_project_team.delay(serialized_policy)

    logger.info(
        "Policy action notify_project_team has been triggered. Policy UUID: %s.",
        policy.uuid.hex,
    )

    log.event_logger.policy_action.info(
        "Cost policy has been triggered and notification to project members has been scheduled.",
        event_type="notify_project_team",
        event_context={"policy_uuid": policy.uuid.hex},
    )


def notify_organization_owners(policy):
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_customer_owners.delay(serialized_policy)

    logger.info(
        "Policy action notify_organization_owners has been triggered. Policy UUID: %s.",
        policy.uuid.hex,
    )

    log.event_logger.policy_action.info(
        "Cost policy has been triggered and notification to organization owners has been scheduled.",
        event_type="notify_organization_owners",
        event_context={"policy_uuid": policy.uuid.hex},
    )


def terminate_resources(policy):
    from waldur_mastermind.marketplace import tasks as marketplace_tasks

    user = get_system_robot()
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.exclude(
        state__in=(
            marketplace_models.Resource.States.TERMINATED,
            marketplace_models.Resource.States.TERMINATING,
        )
    )

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    for resource in resources:
        with transaction.atomic():
            attributes = (
                {"action": "force_destroy"}
                if resource.offering.type == INSTANCE_TYPE
                else {}
            )
            order = marketplace_models.Order.objects.create(
                resource=resource,
                offering=resource.offering,
                type=marketplace_models.Order.Types.TERMINATE,
                state=marketplace_models.Order.States.EXECUTING,
                attributes=attributes,
                project=project,
                created_by=user,
                consumer_reviewed_by=user,
            )

            logger.info(
                "Policy created termination order. Policy UUID: %s. Resource: %s",
                policy.uuid.hex,
                str(resource),
            )

            log.event_logger.policy_action.info(
                "Cost policy has been triggered and termination order has been created. Resource: %s."
                % str(resource),
                event_type="terminate_resources",
                event_context={"policy_uuid": policy.uuid.hex},
            )

            marketplace_tasks.process_order_on_commit(order, user)


def block_creation_of_new_resources(policy, created):
    if created:
        logger.info(
            "Policy action block_creation_of_new_resources has been triggered. Policy UUID: %s.",
            policy.uuid.hex,
        )
        log.event_logger.policy_action.info(
            "Cost policy has been triggered and creation of new resource has been blocked.",
            event_type="block_creation_of_new_resources",
            event_context={"policy_uuid": policy.uuid.hex},
        )
        raise PolicyException(
            "Creation of new resources in this project is not available due to a policy."
        )


def block_modification_of_existing_resources(policy, created):
    if not created:
        logger.info(
            "Policy action block_modification_of_existing_resources has been triggered. Policy UUID: %s.",
            policy.uuid.hex,
        )
        log.event_logger.policy_action.info(
            "Cost policy has been triggered and updating existing resource has been blocked.",
            event_type="block_modification_of_existing_resources",
            event_context={"policy_uuid": policy.uuid.hex},
        )
        raise PolicyException(
            "Modification of new resources in this project is not available due to a policy."
        )


def request_downscaling(policy):
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.exclude(
        state__in=(
            marketplace_models.Resource.States.TERMINATED,
            marketplace_models.Resource.States.TERMINATING,
        )
    )

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    resources.update(downscaled=True)
    logger.info(
        "Policy action request_downscaling has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )
    log.event_logger.policy_action.info(
        "Cost policy has been triggered and downscaling has been requested. Resources: %s"
        % ", ".join([str(r) for r in resources]),
        event_type="block_modification_of_existing_resources",
        event_context={"policy_uuid": policy.uuid.hex},
    )


def reset_downscaling(policy):
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.exclude(
        state__in=(
            marketplace_models.Resource.States.TERMINATED,
            marketplace_models.Resource.States.TERMINATING,
        )
    )

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    resources.update(downscaled=False)
    logger.info(
        "Policy action reset_downscaling has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )


def restrict_members(policy):
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.filter(
        offering__secret_options__service_provider_can_create_offering_user=True
    ).exclude(state__in=(marketplace_models.Resource.States.TERMINATED,))

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    resources.update(restrict_member_access=True)

    logger.info(
        "Policy action restrict_members has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )
    log.event_logger.policy_action.info(
        "Cost policy has been triggered and account removal has been requested. Resources: %s"
        % ", ".join([str(r) for r in resources]),
        event_type="restrict_members",
        event_context={"policy_uuid": policy.uuid.hex},
    )


def reset_member_restriction(policy):
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.filter(
        offering__secret_options__service_provider_can_create_offering_user=True
    ).exclude(state__in=(marketplace_models.Resource.States.TERMINATED,))

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    resources.update(restrict_member_access=False)

    logger.info(
        "Policy action restrict_members has been reset. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([f"{r.name} ({r.slug})" for r in resources]),
    )


def request_pausing(policy):
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.exclude(
        state__in=(marketplace_models.Resource.States.TERMINATED,)
    )

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    resources.update(paused=True)
    logger.info(
        "Policy action request_pausing has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )
    log.event_logger.policy_action.info(
        "Cost policy has been triggered and pausing has been requested. Resources: %s"
        % ", ".join([str(r) for r in resources]),
        event_type="block_modification_of_existing_resources",
        event_context={"policy_uuid": policy.uuid.hex},
    )


def reset_pausing(policy):
    project = structure_permissions._get_project(policy.scope)

    resources = marketplace_models.Resource.objects.exclude(
        state__in=(marketplace_models.Resource.States.TERMINATED,)
    )

    if project:
        resources = resources.filter(project=project)
    else:
        customer = structure_permissions._get_customer(policy.scope)
        resources = resources.filter(project__customer=customer)

    resources.update(paused=False)
    logger.info(
        "Policy action reset_pausing has been triggered. Policy UUID: %s. Resources: %s",
        policy.uuid.hex,
        ", ".join([r.name for r in resources]),
    )


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
}
