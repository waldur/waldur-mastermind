import logging

from django.db import transaction
from rest_framework import exceptions

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import get_system_robot
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_mastermind.policy import tasks

logger = logging.getLogger(__name__)


def notify_project_team(policy):
    serialized_scope = core_utils.serialize_instance(policy.project)
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_about_limit_cost.delay(serialized_scope, serialized_policy)


notify_project_team.one_time_action = True


def notify_organization_owners(policy):
    serialized_scope = core_utils.serialize_instance(policy.project.customer)
    serialized_policy = core_utils.serialize_instance(policy)
    tasks.notify_about_limit_cost.delay(serialized_scope, serialized_policy)


notify_organization_owners.one_time_action = True


def terminate_resources(policy):
    from waldur_mastermind.marketplace import tasks as marketplace_tasks

    user = get_system_robot()

    for resource in marketplace_models.Resource.objects.filter(project=policy.project):
        with transaction.atomic():
            attributes = (
                {'action': 'force_destroy'}
                if resource.offering.type == INSTANCE_TYPE
                else {}
            )
            order_item = marketplace_models.OrderItem(
                resource=resource,
                offering=resource.offering,
                type=marketplace_models.OrderItem.Types.TERMINATE,
                attributes=attributes,
            )
            order = marketplace_models.Order.objects.create(
                project=policy.project, created_by=user
            )
            order_item.order = order
            order_item.save()

            logger.info(
                'Policy created order for terminating resource. Policy UUID: %s. Resource UUID: %s',
                policy.uuid.hex,
                resource.uuid.hex,
            )

            marketplace_tasks.approve_order(order, user)


terminate_resources.one_time_action = True


def block_creation_of_new_resources(policy, created):
    if created:
        raise exceptions.ValidationError(
            'Creation of new resources in this project is not available due to a policy.'
        )


block_creation_of_new_resources.one_time_action = False


def block_modification_of_existing_resources(policy, created):
    if not created:
        raise exceptions.ValidationError(
            'Modification of new resources in this project is not available due to a policy.'
        )


block_modification_of_existing_resources.one_time_action = False


def request_downscaling(policy):
    marketplace_models.Resource.objects.filter(project=policy.project).update(
        requested_downscaling=True
    )


request_downscaling.one_time_action = True
