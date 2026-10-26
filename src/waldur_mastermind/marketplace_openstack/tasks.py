import logging
from typing import cast

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace import callbacks as marketplace_callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)

# Absolute, as in metrics.py: a relative import loads a second model class.
from waldur_mastermind.marketplace_openstack import models
from waldur_openstack import models as openstack_models
from waldur_openstack.exceptions import OpenStackBackendError

from . import metrics, utils

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


@shared_task(
    name="waldur_mastermind.marketplace_openstack.terminate_child_resources_of_terminated_tenants"
)
def terminate_child_resources_of_terminated_tenants():
    """Reconcile Instance/Volume marketplace resources orphaned under a terminated tenant.

    When a tenant's marketplace Resource is force-terminated without the backend
    teardown completing (e.g. a staff ``force_destroy`` on an unreachable/erred
    site, ``marketplace/utils.py`` ``process_order``), the per-tenant
    Instance/Volume child resources are left non-terminated even though their
    parent tenant is gone. A terminated tenant can never legitimately have live
    instances or volumes, so this task marks those child resources TERMINATED to
    keep the marketplace state consistent.

    Mark-only by design: it does NOT delete plugin rows, release quota, or call
    the OpenStack backend (which may be unreachable). The child offerings are
    non-billable, so no invoicing is affected.

    Traceability: for each reconciled resource it records a completed TERMINATE
    ``Order`` (created directly in the DONE state, so no processing/executor and
    no backend call is triggered) carrying a ``reason`` in its attributes, and
    routes the state change through ``callbacks.resource_deletion_succeeded`` so
    the standard ``MARKETPLACE_RESOURCE_TERMINATE_SUCCEEDED`` event appears in the
    resource's audit log.
    """
    tenant_ct = ContentType.objects.get_for_model(openstack_models.Tenant)

    # Tenant marketplace Resources that are TERMINATED (scope = the plugin Tenant).
    terminated_tenant_ids = marketplace_models.Resource.objects.filter(
        offering__type=OPENSTACK_TENANT_OFFERING,
        content_type=tenant_ct,
        state=ResourceStates.TERMINATED,
    ).values_list("object_id", flat=True)

    # Per-tenant Instance/Volume child offerings scoped to those terminated tenants.
    child_offerings = marketplace_models.Offering.objects.filter(
        type__in=utils.PER_TENANT_OFFERING_TYPES,
        content_type=tenant_ct,
        object_id__in=terminated_tenant_ids,
    )

    orphaned_resources = (
        marketplace_models.Resource.objects.filter(
            offering__in=child_offerings,
        )
        .exclude(state=ResourceStates.TERMINATED)
        # Avoid N+1: the loop below reads resource.project, project.customer
        # (audit scopes) and resource.offering (order + logging).
        .select_related("project", "project__customer", "offering")
    )

    reason = (
        "Automatically terminated by the "
        "terminate_child_resources_of_terminated_tenants reconciliation task "
        "because the parent tenant's marketplace resource is already terminated."
    )

    count = 0
    # Client-side chunks: each iteration opens its own transaction below, so
    # a server-side cursor would not survive a transaction-mode pooler.
    for resource in core_utils.chunked_queryset(orphaned_resources):
        try:
            with transaction.atomic():
                # DONE state => no order processing / executor / backend call is
                # triggered (see marketplace.handlers order post_save handlers).
                marketplace_models.Order.objects.create(
                    project=resource.project,
                    resource=resource,
                    offering=resource.offering,
                    type=OrderTypes.TERMINATE,
                    state=OrderStates.DONE,
                    created_by=None,
                    cost=0,
                    attributes={"reason": reason},
                )
                # Flips the resource to TERMINATED and emits the audit event.
                marketplace_callbacks.resource_deletion_succeeded(resource)
        except Exception:
            logger.exception(
                "Failed to terminate orphaned child resource %s (%s).",
                resource.uuid,
                resource.name,
            )
            continue
        count += 1
        logger.info(
            "Terminated orphaned marketplace resource %s (%s) left under terminated tenant offering %s.",
            resource.uuid,
            resource.name,
            resource.offering.name,
        )

    if count:
        logger.info(
            "terminate_child_resources_of_terminated_tenants terminated %s orphaned resource(s).",
            count,
        )


@shared_task(name="waldur_mastermind.marketplace_openstack.collect_instance_metrics")
def collect_instance_metrics():
    """Poll the running instances whose turn it is this minute.

    Runs every minute; each instance comes up once per its offering's polling
    interval, or per the faster one asked for on it, see metrics.is_due. Only
    a few columns of each resource are read: this runs for every instance of
    every provider that collects metrics, every minute.
    """
    now = timezone.now()
    minutes = metrics.minutes_to_dispatch(now)
    if not minutes:
        return
    # Offerings in use only, active or paused: a draft, archived or
    # unavailable one, or its provider's, is left alone.
    rows = list(
        marketplace_models.Resource.objects.filter(
            offering__type=OPENSTACK_INSTANCE_OFFERING,
            offering__state__in=metrics.READ_OFFERING_STATES,
            offering__parent__state__in=metrics.READ_OFFERING_STATES,
            offering__parent__plugin_options__instance_metrics_enabled=True,
            state__in=metrics.READ_RESOURCE_STATES,
            content_type=ContentType.objects.get_for_model(openstack_models.Instance),
        ).values_list("id", "uuid", "object_id", "offering__parent_id")
    )
    providers = marketplace_models.Offering.objects.filter(
        id__in={provider_id for *_, provider_id in rows}
    ).only("id", "parent_id", "plugin_options")
    intervals = {provider.id: metrics.poll_interval(provider) for provider in providers}
    backing_off = {
        provider.id: metrics.is_backing_off(provider) for provider in providers
    }
    schedules = {
        schedule.instance_id: schedule
        for schedule in models.InstanceMetricSchedule.objects.filter(until__gt=now)
    }
    due = {}
    for resource_id, uuid, instance_id, provider_id in rows:
        if backing_off[provider_id]:
            continue
        interval = metrics.current_interval(
            intervals[provider_id], schedules.get(instance_id), now
        )
        if any(metrics.is_due(uuid, interval, minute) for minute in minutes):
            due[instance_id] = (resource_id, interval)
    # Running servers with a sound record, on a provider in working order.
    running = (
        openstack_models.Instance.objects.filter(
            id__in=due,
            runtime_state=openstack_models.Instance.RuntimeStates.ACTIVE,
            state__in=metrics.READ_INSTANCE_STATES,
            tenant__service_settings__is_active=True,
        )
        .exclude(backend_id="")
        .exclude(tenant__service_settings__state=CoreStates.ERRED)
        .values_list("id", flat=True)
    )
    for instance_id in running:
        resource_id, interval = due[instance_id]
        collect_resource_metrics.apply_async(
            args=(resource_id,), expires=metrics.poll_expiry(interval)
        )


# Background queue: polls must not hold up orders and other user-facing work.
@shared_task(
    name="waldur_mastermind.marketplace_openstack.collect_resource_metrics",
    is_background=True,
)
def collect_resource_metrics(resource_id):
    try:
        resource = marketplace_models.Resource.objects.get(pk=resource_id)
    except marketplace_models.Resource.DoesNotExist:
        return
    try:
        metrics.collect(resource)
    except OpenStackBackendError as e:
        if metrics.is_instance_error(e):
            # Stopped or deleted since the last pull, or no diagnostics there.
            logger.info("Instance of resource %s cannot be read: %s", resource, e)
        else:
            logger.warning("Could not read resource %s from Nova: %s", resource, e)


@shared_task(
    name="waldur_mastermind.marketplace_openstack.sync_instance_metric_offerings"
)
def sync_instance_metric_offerings(offering_id):
    """Apply a provider's metrics choice to every one of its tenants."""
    for tenant_offering in marketplace_models.Offering.objects.filter(
        parent_id=offering_id,
        type__in=(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING),
    ).select_related("parent"):
        try:
            metrics.sync_tenant_offering(tenant_offering)
        except Exception:
            # One tenant's failure doesn't leave the others unsynced.
            logger.exception(
                "Could not sync the instance metrics of offering %s.", tenant_offering
            )
