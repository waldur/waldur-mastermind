import logging
from typing import cast

from celery import shared_task
from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openstack import models as openstack_models

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
    """Heal marketplace state for every tenant that has orphan OpenStack
    instances or volumes.

    Previously this task called create_marketplace_resource_for_imported_resources
    directly, which silently no-ops when the per-tenant Instance/Volume offering
    is missing. That left orphan VMs invisible in the marketplace UI even after
    the periodic task ran. Routing through self_heal_tenant_marketplace_model
    recreates archived/missing per-tenant offerings first, then backfills the
    orphan resources — so tenants no longer need a manual "Synchronise" click
    after the offering-side state drifts.
    """
    affected_tenant_ids: set[int] = set()
    for klass in (openstack_models.Instance, openstack_models.Volume):
        content_type = ContentType.objects.get_for_model(klass)
        linked_ids = set(
            marketplace_models.Resource.objects.filter(
                content_type=content_type
            ).values_list("object_id", flat=True)
        )
        affected_tenant_ids.update(
            klass.objects.exclude(id__in=linked_ids)
            .exclude(tenant__isnull=True)
            .values_list("tenant_id", flat=True)
            .distinct()
        )

    if not affected_tenant_ids:
        return

    for tenant in openstack_models.Tenant.objects.filter(id__in=affected_tenant_ids):
        try:
            utils.self_heal_tenant_marketplace_model(tenant)
        except Exception:
            logger.exception(
                "Periodic self-heal failed for tenant %s; continuing.",
                tenant.id,
            )


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
