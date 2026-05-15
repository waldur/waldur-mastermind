import logging
from typing import cast

from celery import shared_task
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openstack import models as openstack_models

from ..marketplace.enums import OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING
from . import utils

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.marketplace_openstack.push_tenant_limits")
def push_tenant_limits(serialized_resource: str):
    resource = cast(
        marketplace_models.Resource,
        core_utils.deserialize_instance(serialized_resource),
    )
    utils.push_tenant_limits(resource)


@shared_task(name="waldur_mastermind.marketplace_openstack.restore_tenant_limits")
def restore_tenant_limits(serialized_resource: str):
    resource = cast(
        marketplace_models.Resource,
        core_utils.deserialize_instance(serialized_resource),
    )
    utils.restore_limits(resource)


@shared_task(
    name="waldur_mastermind.marketplace_openstack.import_instances_and_volumes_of_tenant"
)
def sync_instances_and_volumes_of_tenant(serialized_resource: str):
    resource = cast(
        openstack_models.Tenant,
        core_utils.deserialize_instance(serialized_resource),
    )
    try:
        utils.self_heal_tenant_marketplace_model(resource)
    except Exception:
        logger.exception(
            "Self-heal of marketplace model failed for tenant %s; continuing with import.",
            resource.id,
        )
    utils.import_instances_and_volumes_of_tenant(resource)
    utils.terminate_expired_instances_and_volumes_of_tenant(resource)


@shared_task(
    name="waldur_mastermind.marketplace_openstack.create_resources_for_lost_instances_and_volumes"
)
def create_resources_for_lost_instances_and_volumes():
    """Create marketplace resources for OpenStack instances and volumes that exist in backend but are missing from marketplace."""
    for offering_type, klass in (
        (OPENSTACK_INSTANCE_OFFERING, openstack_models.Instance),
        (OPENSTACK_VOLUME_OFFERING, openstack_models.Volume),
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
    """Refresh metadata for OpenStack instances from backend to ensure marketplace resources have up-to-date information."""
    for instance in openstack_models.Instance.objects.all():
        try:
            resource = marketplace_models.Resource.objects.get(scope=instance)
        except marketplace_models.Resource.DoesNotExist:
            # Instance has not been promoted to a marketplace Resource yet
            # (race with create_resources_for_lost_instances_and_volumes,
            # or imported instance not yet linked). Skip silently.
            continue
        utils.import_instance_metadata(resource)
