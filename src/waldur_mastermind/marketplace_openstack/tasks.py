import logging

from celery import shared_task
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

from . import INSTANCE_TYPE, VOLUME_TYPE, utils

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.marketplace_openstack.push_tenant_limits")
def push_tenant_limits(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.push_tenant_limits(resource)


@shared_task(name="waldur_mastermind.marketplace_openstack.restore_tenant_limits")
def restore_tenant_limits(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.restore_limits(resource)


@shared_task(
    name="waldur_mastermind.marketplace_openstack.import_instances_and_volumes_of_tenant"
)
def sync_instances_and_volumes_of_tenant(serialized_resource):
    resource = core_utils.deserialize_instance(serialized_resource)
    utils.import_instances_and_volumes_of_tenant(resource)
    utils.terminate_expired_instances_and_volumes_of_tenant(resource)


@shared_task(
    name="waldur_mastermind.marketplace_openstack.create_resources_for_lost_instances_and_volumes"
)
def create_resources_for_lost_instances_and_volumes():
    for offering_type, klass in (
        (INSTANCE_TYPE, openstack_tenant_models.Instance),
        (VOLUME_TYPE, openstack_tenant_models.Volume),
    ):
        ids = marketplace_models.Resource.objects.filter(
            offering__type=offering_type
        ).values_list("object_id", flat=True)
        instances = klass.objects.exclude(id__in=ids)

        for instance in instances:
            try:
                utils.create_marketplace_resource_for_imported_resources(instance)
            except (ObjectDoesNotExist, MultipleObjectsReturned):
                continue


@shared_task(
    name="waldur_mastermind.marketplace_openstack.refresh_instance_backend_metadata"
)
def refresh_instance_backend_metadata():
    instances = marketplace_models.Resource.objects.filter(offering__type=INSTANCE_TYPE)
    for instance in instances:
        resource = marketplace_models.Resource.objects.get(scope=instance)
        utils.import_instance_metadata(resource)
