import ipaddress
import logging
from typing import cast

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.core import validators as core_validators
from waldur_core.core.enums import CoreStates
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure.backend import ServiceBackend
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import plugins
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.utils import (
    get_resource_state,
    import_current_usages,
    import_resource_metadata,
)
from waldur_mastermind.marketplace_openstack import (
    BILLING_SOURCE_PLACEMENT,
    BILLING_SOURCE_QUOTA,
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
)
from waldur_openstack import (
    executors as openstack_executors,
)
from waldur_openstack import (
    models as openstack_models,
)
from waldur_openstack.backend import OpenStackBackend, OpenStackBackendError
from waldur_openstack.session import get_placement_client
from waldur_openstack.utils import (
    is_valid_volume_type_name,
    volume_type_name_to_quota_name,
)

logger = logging.getLogger(__name__)
TenantQuotas = openstack_models.Tenant.Quotas


def get_offering_name_for_instance(tenant):
    return "Virtual machine in %s" % tenant.name


def get_offering_category_for_instance():
    category, created = marketplace_models.Category.objects.get_or_create(
        default_vm_category=True,
        defaults={"title": "Virtual machines"},
    )
    if created:
        logger.info("Created default VM category: %s", category)
    return category


def get_offering_name_for_volume(tenant):
    return "Volume in %s" % tenant.name


def get_offering_category_for_volume():
    category, created = marketplace_models.Category.objects.get_or_create(
        default_volume_category=True,
        defaults={"title": "Volumes"},
    )
    if created:
        logger.info("Created default Volume category: %s", category)
    return category


def get_category_and_name_for_offering_type(offering_type, tenant):
    if offering_type == OPENSTACK_INSTANCE_OFFERING:
        category = get_offering_category_for_instance()
        name = get_offering_name_for_instance(tenant)
        return category, name
    elif offering_type == OPENSTACK_VOLUME_OFFERING:
        category = get_offering_category_for_volume()
        name = get_offering_name_for_volume(tenant)
        return category, name


def create_offering_components(offering):
    fixed_components = plugins.manager.get_components(OPENSTACK_TENANT_OFFERING)

    for component_data in fixed_components:
        marketplace_models.OfferingComponent.objects.create(
            offering=offering, **component_data._asdict()
        )


def import_volume_metadata(resource: marketplace_models.Resource):
    import_resource_metadata(resource)
    volume = cast(openstack_models.Volume, resource.scope)
    resource.backend_metadata["size"] = volume.size

    if volume.instance:
        resource.backend_metadata["instance_uuid"] = volume.instance.uuid.hex
        resource.backend_metadata["instance_name"] = volume.instance.name
    else:
        resource.backend_metadata["instance_uuid"] = None
        resource.backend_metadata["instance_name"] = None

    if volume.type:
        resource.backend_metadata["type_name"] = volume.type.name
    else:
        resource.backend_metadata["type_name"] = None

    resource.save(update_fields=["backend_metadata"])


def import_instance_metadata(resource: marketplace_models.Resource):
    import_resource_metadata(resource)
    instance = cast(openstack_models.Instance, resource.scope)
    resource.backend_metadata["internal_ips"] = instance.internal_ips
    resource.backend_metadata["external_ips"] = instance.external_ips
    resource.backend_metadata["flavor_name"] = instance.flavor_name
    resource.backend_metadata["image_name"] = instance.image_name
    bootable_volume = instance.volumes.filter(bootable=True).first()
    if bootable_volume and (bootable_volume.image or bootable_volume.image_name):
        resource.backend_metadata["system_volume_image_name"] = (
            bootable_volume.image_name or bootable_volume.image.name
        )
    resource.save(update_fields=["backend_metadata"])


def get_offering(offering_type, scope):
    try:
        return marketplace_models.Offering.objects.get(scope=scope, type=offering_type)
    except ObjectDoesNotExist:
        logger.warning(
            "Marketplace offering is not found. Scope: %s",
            scope,
        )
    except MultipleObjectsReturned:
        logger.warning(
            "Multiple marketplace offerings are found. Scope: %s",
            scope,
        )


def import_quotas(offering: marketplace_models.Offering, source_values):
    storage_mode = offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED

    result_values = {
        CORES_TYPE: source_values.get(TenantQuotas.vcpu.name, 0),
        RAM_TYPE: source_values.get(TenantQuotas.ram.name, 0),
    }

    if storage_mode == STORAGE_MODE_FIXED:
        result_values[STORAGE_TYPE] = source_values.get(TenantQuotas.storage.name, 0)
    elif storage_mode == STORAGE_MODE_DYNAMIC:
        volume_type_values = {
            k: v for (k, v) in source_values.items() if is_valid_volume_type_name(k)
        }
        result_values.update(volume_type_values)

    return result_values


def _apply_quotas(target: openstack_models.Tenant, quotas: dict[str, int]):
    for name, limit in quotas.items():
        target.set_quota_limit(name, limit)


def get_usage_values(resource: marketplace_models.Resource, tenant) -> dict:
    """Build the ``{component_type: usage}`` dict for a tenant resource.

    Default (``billing_source`` unset or ``"quota"``) sources cores/ram/storage
    from the tenant's flavor-derived Nova quota and Cinder quota usage. With
    ``plugin_options["billing_source"] == "placement"``, cores/ram and
    specialty resource classes (VGPU, PCI_DEVICE, custom) come from Placement
    allocations instead; storage stays on Cinder quota (Placement ``DISK_GB`` is
    ephemeral disk, not Cinder volumes — see ``PLACEMENT_CLASSES_IGNORED``).
    """
    quota_usages = import_quotas(resource.offering, tenant.quota_usages)
    billing_source = (
        resource.offering.plugin_options.get("billing_source") or BILLING_SOURCE_QUOTA
    )
    if billing_source != BILLING_SOURCE_PLACEMENT:
        return quota_usages

    try:
        placement_totals = collect_placement_allocations(tenant)
    except OpenStackBackendError as e:
        # A transient Placement outage must not zero the bill: fall back to the
        # quota-derived numbers for this tick. The high-watermark in
        # import_current_usages keeps the monthly peak intact regardless.
        logger.warning(
            "Placement billing source unavailable for tenant %s; "
            "falling back to quota usage: %s",
            tenant,
            e,
        )
        return quota_usages
    return merge_placement_usages(quota_usages, placement_totals)


def import_usage(resource: marketplace_models.Resource):
    tenant = cast(openstack_models.Tenant, resource.scope)

    if not tenant:
        return

    usages = get_usage_values(resource, tenant)
    has_usage_billing = resource.offering.components.filter(
        billing_type=BillingTypes.USAGE,
    ).exists()
    # Update ComponentUsage for billing (monthly peak for LIMIT components,
    # hourly accumulator for USAGE components).
    import_current_usages(resource, usages, hourly_accumulation=has_usage_billing)
    # current_usages drives the UI's "right-now consumption" widget. Write
    # the fresh values directly — the ComponentUsage mirror would otherwise
    # latch at the monthly peak or grow with cumulative core-hours, neither
    # of which is the at-a-glance number the user expects.
    resource.refresh_from_db(fields=["current_usages"])
    current = dict(resource.current_usages or {})
    current.update({k: float(v) for k, v in usages.items()})
    resource.current_usages = current
    resource.last_sync = timezone.now()
    resource.save(update_fields=["current_usages", "last_sync"])


def import_limits(resource: marketplace_models.Resource):
    """
    Import resource quotas as marketplace limits.
    :param resource: Marketplace resource
    """
    tenant = cast(openstack_models.Tenant, resource.scope)

    if not tenant:
        return

    resource.limits = import_quotas(resource.offering, tenant.quota_limits)
    resource.save(update_fields=["limits"])


def tenant_limits_validator(limits: dict):
    cores = limits.get(CORES_TYPE) or 0
    if not cores:
        raise exceptions.ValidationError("CPU limit is mandatory.")

    ram = limits.get(RAM_TYPE) or 0
    if not ram:
        raise exceptions.ValidationError("RAM limit is mandatory.")

    storage = sum(
        value
        for key, value in limits.items()
        if is_valid_volume_type_name(key) or key == STORAGE_TYPE
    )
    if not storage:
        raise exceptions.ValidationError("Storage limit is mandatory.")


def map_limits_to_quotas(limits, offering: marketplace_models.Offering, is_create=True):
    quotas = {
        TenantQuotas.vcpu.name: limits.get(CORES_TYPE),
        TenantQuotas.ram.name: limits.get(RAM_TYPE),
    }

    if is_create:
        quotas.update(
            {
                TenantQuotas.instances.name: offering.plugin_options.get(
                    "max_instances"
                ),
                TenantQuotas.volumes.name: offering.plugin_options.get("max_volumes"),
                TenantQuotas.security_group_count.name: offering.plugin_options.get(
                    "max_security_groups"
                ),
            }
        )

    quotas = {k: v for k, v in quotas.items() if v is not None}

    storage_mode = offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
    if storage_mode == STORAGE_MODE_FIXED:
        quotas[TenantQuotas.storage.name] = limits.get(STORAGE_TYPE)
    else:
        # Filter volume-type quotas.
        volume_type_quotas = dict(
            (key, value)
            for (key, value) in limits.items()
            if is_valid_volume_type_name(key) and value is not None
        )

        # Initialize volume type quotas as zero, otherwise they are treated as unlimited
        for volume_type in openstack_models.VolumeType.objects.filter(
            settings=offering.scope
        ):
            volume_type_quotas.setdefault(
                volume_type_name_to_quota_name(volume_type.name), 0
            )
        quotas.update(volume_type_quotas)

        # Common storage quota should be equal to sum of all volume-type quotas.
        quotas["storage"] = ServiceBackend.gb2mb(sum(list(volume_type_quotas.values())))

    # Convert quota value from float to integer because OpenStack API fails otherwise
    return {k: int(v) for k, v in quotas.items() if v is not None}


def update_limits(order: marketplace_models.Order):
    tenant = cast(openstack_models.Tenant, order.resource.scope)
    backend = tenant.get_backend()
    quotas = map_limits_to_quotas(order.limits, order.offering, is_create=False)
    backend.push_tenant_quotas(tenant, quotas)
    with transaction.atomic():
        _apply_quotas(tenant, quotas)


def import_limits_when_storage_mode_is_switched(resource: marketplace_models.Resource):
    tenant = cast(openstack_models.Tenant, resource.scope)

    if not tenant:
        return

    storage_mode = (
        resource.offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
    )

    raw_limits = tenant.quota_limits
    raw_usages = tenant.quota_usages

    limits = {
        CORES_TYPE: raw_limits.get(TenantQuotas.vcpu.name, 0),
        RAM_TYPE: raw_limits.get(TenantQuotas.ram.name, 0),
    }

    if storage_mode == STORAGE_MODE_FIXED:
        limits[STORAGE_TYPE] = raw_usages.get(TenantQuotas.storage.name, 0)
    elif storage_mode == STORAGE_MODE_DYNAMIC:
        volume_type_limits = {
            k: v for (k, v) in raw_usages.items() if is_valid_volume_type_name(k)
        }
        limits.update(volume_type_limits)

    resource.limits = limits
    resource.save(update_fields=["limits"])


def push_tenant_limits(resource: marketplace_models.Resource):
    tenant = cast(openstack_models.Tenant, resource.scope)
    backend = tenant.get_backend()
    quotas = map_limits_to_quotas(resource.limits, resource.offering, is_create=False)
    backend.push_tenant_quotas(tenant, quotas)
    with transaction.atomic():
        _apply_quotas(tenant, quotas)


def restore_limits(resource: marketplace_models.Resource):
    order = (
        marketplace_models.Order.objects.filter(
            resource=resource,
            type__in=[
                OrderTypes.CREATE,
                OrderTypes.UPDATE,
            ],
        )
        .order_by("-created")
        .first()
    )

    if not order:
        return

    if not isinstance(order.resource.scope, openstack_models.Tenant):
        return

    update_limits(order)


def count_active_resources(offering: marketplace_models.Offering) -> int:
    """Number of non-terminated marketplace Resources attached to an offering."""
    return (
        marketplace_models.Resource.objects.filter(offering=offering)
        .exclude(state=marketplace_models.Resource.States.TERMINATED)
        .count()
    )


def describe_offering_candidates(
    offerings: list[marketplace_models.Offering],
) -> str:
    """Human-readable, operator-facing breakdown of duplicate offerings.

    Emits ``id``, ``name``, lifecycle ``state`` and the count of non-terminated
    resources for each candidate so an operator can tell at a glance which
    offering is in use and which are safe to drop. Used both in the self-heal
    ERROR log and by the ``dedupe_tenant_offerings`` command.
    """
    parts = []
    for offering in sorted(offerings, key=lambda o: o.id):
        parts.append(
            "id=%s name=%r state=%s active_resources=%d total_resources=%d"
            % (
                offering.id,
                offering.name,
                offering.get_state_display(),
                count_active_resources(offering),
                marketplace_models.Resource.objects.filter(offering=offering).count(),
            )
        )
    return "; ".join(parts)


# Per-tenant offering types that self-heal expects exactly one of per tenant.
PER_TENANT_OFFERING_TYPES = (OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)

# Maps each per-tenant offering type to the OpenStack resource class it governs,
# so orphan resources (rows with no marketplace Resource) can be counted per type.
_OFFERING_TYPE_TO_RESOURCE_CLASS = {
    OPENSTACK_INSTANCE_OFFERING: openstack_models.Instance,
    OPENSTACK_VOLUME_OFFERING: openstack_models.Volume,
}


def collect_duplicate_offering_groups(tenant_id: int | None = None) -> dict:
    """Return ``{(tenant_object_id, offering_type): [offerings]}`` for groups > 1.

    Per-tenant Instance/Volume offerings are always scoped to a Tenant, so the
    ScopeMixin ``object_id`` is the tenant primary key (equal to the ``tenant.id``
    printed by self-heal). Grouping on it avoids loading each generic-FK scope.
    Shared by the ``dedupe_tenant_offerings`` command and the read-only
    diagnostics API so both agree on what "duplicate" means.
    """
    qs = marketplace_models.Offering.objects.filter(
        type__in=PER_TENANT_OFFERING_TYPES,
        object_id__isnull=False,
    )
    if tenant_id is not None:
        qs = qs.filter(object_id=tenant_id)
    groups: dict = {}
    for offering in qs:
        key = (offering.object_id, offering.type)
        groups.setdefault(key, []).append(offering)
    return {key: value for key, value in groups.items() if len(value) > 1}


def pick_keeper_offering(
    offerings: list[marketplace_models.Offering],
) -> marketplace_models.Offering:
    """Choose which offering in a duplicate group to keep.

    Preference order: most non-terminated resources, then an active lifecycle
    state, then the oldest (lowest id) — i.e. the original offering.
    """
    return max(
        offerings,
        key=lambda o: (
            count_active_resources(o),
            o.state == marketplace_models.Offering.States.ACTIVE,
            -o.id,
        ),
    )


def count_tenant_orphan_resources(tenant: openstack_models.Tenant, klass) -> int:
    """Number of ``klass`` rows in ``tenant`` with no marketplace Resource.

    These are exactly the orphans self-heal cannot link while the per-tenant
    offering is ambiguous — the user-visible symptom (VMs/volumes missing from
    the marketplace). Same query as ``self_heal_tenant_orphan_resources``.
    """
    content_type = ContentType.objects.get_for_model(klass)
    linked_ids = set(
        marketplace_models.Resource.objects.filter(
            content_type=content_type
        ).values_list("object_id", flat=True)
    )
    return klass.objects.filter(tenant=tenant).exclude(id__in=linked_ids).count()


def build_duplicate_offering_report(tenant_id: int | None = None) -> list[dict]:
    """Structured, read-only report of tenants with duplicate per-tenant offerings.

    One row per (tenant, offering type) group, each carrying the candidate
    offerings (with the recommended keeper flagged), the tenant/customer
    identity, and the count of orphaned resources of that type. Consumed by the
    staff diagnostics API; mirrors the fields ``describe_offering_candidates``
    formats into a log string.
    """
    report = []
    groups = collect_duplicate_offering_groups(tenant_id)
    for (object_id, offering_type), offerings in sorted(groups.items()):
        keeper = pick_keeper_offering(offerings)
        tenant = offerings[0].scope
        tenant_uuid = tenant_name = customer_name = customer_uuid = None
        orphan_count = 0
        if tenant is not None:
            tenant_uuid = getattr(tenant, "uuid", None)
            tenant_name = tenant.name
            project = getattr(tenant, "project", None)
            if project is not None:
                customer_name = project.customer.name
                customer_uuid = project.customer.uuid
            klass = _OFFERING_TYPE_TO_RESOURCE_CLASS.get(offering_type)
            if klass is not None:
                orphan_count = count_tenant_orphan_resources(tenant, klass)
        candidates = [
            {
                "id": offering.id,
                "uuid": offering.uuid,
                "name": offering.name,
                "state": offering.get_state_display(),
                "active_resources": count_active_resources(offering),
                "total_resources": marketplace_models.Resource.objects.filter(
                    offering=offering
                ).count(),
                "is_recommended_keeper": offering.id == keeper.id,
            }
            for offering in sorted(offerings, key=lambda o: o.id)
        ]
        report.append(
            {
                "tenant_id": object_id,
                "tenant_uuid": tenant_uuid,
                "tenant_name": tenant_name,
                "customer_name": customer_name,
                "customer_uuid": customer_uuid,
                "offering_type": offering_type,
                "recommended_keeper_id": keeper.id,
                "orphan_count": orphan_count,
                "candidates": candidates,
            }
        )
    return report


def _duplicate_is_empty(duplicate: marketplace_models.Offering) -> bool:
    """A duplicate nothing ever attached to — safe to delete outright."""
    return (
        not marketplace_models.Resource.objects.filter(offering=duplicate).exists()
        and not marketplace_models.Order.objects.filter(offering=duplicate).exists()
    )


def plan_offering_merge(
    duplicate: marketplace_models.Offering,
    keeper: marketplace_models.Offering,
) -> dict:
    """Preflight a merge of ``duplicate`` into ``keeper``. Performs no writes.

    Every FK to Offering is ``on_delete=CASCADE``, so deleting the duplicate
    also destroys rows that belong to the resources being moved. Re-pointing
    only Resource/Order (what this used to do) silently discarded billing and
    usage history:

    * ``Plan.offering`` -> ``ResourcePlanPeriod.plan`` (billing periods)
    * ``OfferingComponent.offering`` -> ``ComponentUsage`` / ``ComponentQuota``

    Those two need a counterpart on the keeper, and ``ResourcePlanPeriod.plan``
    is not nullable, so an unmatched plan cannot simply be dropped. Anything
    that cannot be re-pointed is returned as a blocker and the caller must
    refuse rather than delete history.
    """
    resources = marketplace_models.Resource.objects.filter(offering=duplicate)
    orders = marketplace_models.Order.objects.filter(offering=duplicate)
    resource_ids = list(resources.values_list("id", flat=True))

    plan_periods = marketplace_models.ResourcePlanPeriod.objects.filter(
        resource_id__in=resource_ids, plan__offering=duplicate
    )
    usages = marketplace_models.ComponentUsage.objects.filter(
        resource_id__in=resource_ids, component__offering=duplicate
    )
    quotas = marketplace_models.ComponentQuota.objects.filter(
        resource_id__in=resource_ids, component__offering=duplicate
    )

    keeper_plan_names = set(keeper.plans.values_list("name", flat=True))
    keeper_component_types = set(keeper.components.values_list("type", flat=True))

    blockers = []
    missing_plan_names = sorted(
        set(plan_periods.values_list("plan__name", flat=True)) - keeper_plan_names
    )
    for name in missing_plan_names:
        blockers.append(
            f"Keeper has no plan named {name!r}, required by "
            f"{plan_periods.filter(plan__name=name).count()} billing period(s)."
        )
    missing_component_types = sorted(
        (
            set(usages.values_list("component__type", flat=True))
            | set(quotas.values_list("component__type", flat=True))
        )
        - keeper_component_types
    )
    for component_type in missing_component_types:
        blockers.append(
            f"Keeper has no component of type {component_type!r}, required by "
            "usage or quota records."
        )

    return {
        "duplicate_id": duplicate.id,
        "duplicate_name": duplicate.name,
        "keeper_id": keeper.id,
        "keeper_name": keeper.name,
        "is_empty": not resource_ids and not orders.exists(),
        "resource_count": len(resource_ids),
        "order_count": orders.count(),
        "plan_period_count": plan_periods.count(),
        "component_usage_count": usages.count(),
        "component_quota_count": quotas.count(),
        "blockers": blockers,
    }


class OfferingMergeBlocked(Exception):
    """Raised when a merge would discard history that cannot be re-pointed."""

    def __init__(self, blockers: list[str]):
        self.blockers = blockers
        super().__init__("; ".join(blockers))


def merge_duplicate_offering(
    duplicate: marketplace_models.Offering,
    keeper: marketplace_models.Offering,
) -> dict:
    """Re-point everything owned by ``duplicate`` onto ``keeper``, then delete it.

    Raises ``OfferingMergeBlocked`` if the preflight found history that cannot
    be preserved. Everything happens in one transaction, and the preflight is
    re-run inside it: the caller may have previewed the merge minutes earlier
    and a resource could have attached since (TOCTOU).
    """
    with transaction.atomic():
        # Lock the pair so a concurrent merge/attach cannot race this one.
        marketplace_models.Offering.objects.select_for_update().filter(
            id__in=[duplicate.id, keeper.id]
        ).exists()

        result = plan_offering_merge(duplicate, keeper)
        if result["blockers"]:
            raise OfferingMergeBlocked(result["blockers"])

        keeper_plans_by_name = {plan.name: plan for plan in keeper.plans.all()}
        keeper_components_by_type = {
            component.type: component for component in keeper.components.all()
        }

        resources = marketplace_models.Resource.objects.filter(offering=duplicate)
        resource_ids = list(resources.values_list("id", flat=True))

        # Re-point the history first: it is selected via the duplicate's plans
        # and components, which stop matching once the resources have moved.
        for plan_period in marketplace_models.ResourcePlanPeriod.objects.filter(
            resource_id__in=resource_ids, plan__offering=duplicate
        ).select_related("plan"):
            plan_period.plan = keeper_plans_by_name[plan_period.plan.name]
            plan_period.save(update_fields=["plan"])

        for usage in marketplace_models.ComponentUsage.objects.filter(
            resource_id__in=resource_ids, component__offering=duplicate
        ).select_related("component"):
            usage.component = keeper_components_by_type[usage.component.type]
            usage.save(update_fields=["component"])

        for quota in marketplace_models.ComponentQuota.objects.filter(
            resource_id__in=resource_ids, component__offering=duplicate
        ).select_related("component"):
            target = keeper_components_by_type[quota.component.type]
            # (resource, component) is unique: if the resource already carries a
            # quota for the keeper's component, the duplicate's row is redundant.
            if (
                marketplace_models.ComponentQuota.objects.filter(
                    resource_id=quota.resource_id, component=target
                )
                .exclude(id=quota.id)
                .exists()
            ):
                quota.delete()
                continue
            quota.component = target
            quota.save(update_fields=["component"])

        # Resource.plan / Order.plan are nullable, so an unmatched plan falls
        # back to null rather than leaving a dangling cross-offering reference.
        def remap_plan(plan):
            if plan is None:
                return None
            return keeper_plans_by_name.get(plan.name)

        for resource in resources.select_related("plan"):
            resource.offering = keeper
            resource.plan = remap_plan(resource.plan)
            resource.save(update_fields=["offering", "plan"])

        for order in marketplace_models.Order.objects.filter(
            offering=duplicate
        ).select_related("plan"):
            order.offering = keeper
            order.plan = remap_plan(order.plan)
            order.save(update_fields=["offering", "plan"])

        logger.info(
            "Merged duplicate offering id=%s into keeper id=%s for tenant %s "
            "(%s resources, %s orders, %s plan periods, %s usages, %s quotas "
            "re-pointed).",
            duplicate.id,
            keeper.id,
            duplicate.object_id,
            result["resource_count"],
            result["order_count"],
            result["plan_period_count"],
            result["component_usage_count"],
            result["component_quota_count"],
        )
        duplicate.delete()

    return result


def delete_empty_duplicate_offering(duplicate: marketplace_models.Offering) -> None:
    """Delete a duplicate that owns nothing. Re-checks emptiness under lock."""
    with transaction.atomic():
        marketplace_models.Offering.objects.select_for_update().filter(
            id=duplicate.id
        ).exists()
        if not _duplicate_is_empty(duplicate):
            raise OfferingMergeBlocked(
                [
                    f"Offering id={duplicate.id} is no longer empty; "
                    "re-run the report and merge instead."
                ]
            )
        logger.info(
            "Deleting empty duplicate offering id=%s name=%r for tenant %s.",
            duplicate.id,
            duplicate.name,
            duplicate.object_id,
        )
        duplicate.delete()


def remediate_duplicate_offering_group(
    tenant_id: int,
    offering_type: str,
    dry_run: bool = True,
    merge: bool = True,
) -> dict:
    """Resolve one (tenant, offering type) duplicate group down to its keeper.

    The keeper is resolved server-side from the same helpers that build the
    report, so a caller cannot direct the merge at a different offering.
    """
    groups = collect_duplicate_offering_groups(tenant_id)
    offerings = groups.get((tenant_id, offering_type))
    if not offerings:
        raise OfferingMergeBlocked(
            [
                f"No duplicate {offering_type} offerings found for tenant "
                f"{tenant_id}; the report may be out of date."
            ]
        )

    keeper = pick_keeper_offering(offerings)
    duplicates = [o for o in offerings if o.id != keeper.id]
    plans = [plan_offering_merge(duplicate, keeper) for duplicate in duplicates]

    for plan in plans:
        if plan["is_empty"]:
            plan["action"] = "delete"
        elif not merge:
            plan["action"] = "skip"
            plan["blockers"] = plan["blockers"] or [
                "Still owns resources or orders; merging is required to resolve it."
            ]
        else:
            plan["action"] = "merge"

    blocked = [plan for plan in plans if plan["blockers"]]
    result = {
        "tenant_id": tenant_id,
        "offering_type": offering_type,
        "keeper_id": keeper.id,
        "keeper_name": keeper.name,
        "dry_run": dry_run,
        "duplicates": plans,
        "blockers": [b for plan in blocked for b in plan["blockers"]],
    }
    if dry_run or blocked:
        return result

    for plan in plans:
        duplicate = next(o for o in duplicates if o.id == plan["duplicate_id"])
        if plan["action"] == "delete":
            delete_empty_duplicate_offering(duplicate)
        elif plan["action"] == "merge":
            merge_duplicate_offering(duplicate, keeper)
    return result


def self_heal_tenant_offerings(tenant: openstack_models.Tenant) -> dict:
    """Ensure per-tenant Instance/Volume offerings exist and are usable.

    Returns a dict mapping offering type to the action taken: one of
    "ok", "unarchived", "recreated", "skipped_no_parent",
    "skipped_multiple", "skipped_disabled".
    """
    result: dict = {
        OPENSTACK_INSTANCE_OFFERING: "ok",
        OPENSTACK_VOLUME_OFFERING: "ok",
    }

    auto_create = settings.WALDUR_MARKETPLACE_OPENSTACK[
        "AUTOMATICALLY_CREATE_PRIVATE_OFFERING"
    ]

    needs_creation = []
    for offering_type in (OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING):
        offerings = list(
            marketplace_models.Offering.objects.filter(type=offering_type, scope=tenant)
        )
        if len(offerings) == 0:
            if not auto_create:
                result[offering_type] = "skipped_disabled"
                logger.info(
                    "Skipping per-tenant %s offering recreation: "
                    "AUTOMATICALLY_CREATE_PRIVATE_OFFERING is False. "
                    "OpenStack tenant ID: %s",
                    offering_type,
                    tenant.id,
                )
                continue
            needs_creation.append(offering_type)
        elif len(offerings) == 1:
            offering = offerings[0]
            if offering.state == OfferingStates.ARCHIVED:
                offering.state = OfferingStates.ACTIVE
                offering.save(update_fields=["state"])
                result[offering_type] = "unarchived"
                logger.info(
                    "Self-heal unarchived per-tenant %s offering for tenant %s",
                    offering_type,
                    tenant.id,
                )
        else:
            result[offering_type] = "skipped_multiple"
            logger.error(
                "Self-heal skipped: %d per-tenant %s offerings exist for tenant "
                "%s (expected exactly 1). Candidates: %s. Resolve with "
                "'dedupe_tenant_offerings --tenant %s' (dry-run) — it keeps the "
                "offering with attached resources and removes the empty "
                "duplicate(s).",
                len(offerings),
                offering_type,
                tenant.id,
                describe_offering_candidates(offerings),
                tenant.id,
            )

    if needs_creation:
        try:
            resource = marketplace_models.Resource.objects.get(scope=tenant)
        except ObjectDoesNotExist:
            for offering_type in needs_creation:
                result[offering_type] = "skipped_no_parent"
            logger.error(
                "Self-heal skipped: tenant %s has no marketplace Resource — "
                "cannot derive parent offering for per-tenant Instance/Volume "
                "offerings. Operator action required.",
                tenant.id,
            )
            return result

        if resource.offering is None:
            for offering_type in needs_creation:
                result[offering_type] = "skipped_no_parent"
            logger.error(
                "Self-heal skipped: tenant %s marketplace Resource has no "
                "parent offering. Operator action required.",
                tenant.id,
            )
            return result

        create_offerings_for_volume_and_instance(tenant)
        for offering_type in needs_creation:
            if marketplace_models.Offering.objects.filter(
                type=offering_type, scope=tenant
            ).exists():
                result[offering_type] = "recreated"
                logger.info(
                    "Self-heal recreated per-tenant %s offering for tenant %s",
                    offering_type,
                    tenant.id,
                )

    return result


_BROKEN_OFFERING_ACTIONS = frozenset(
    {"skipped_multiple", "skipped_no_parent", "skipped_disabled"}
)


def self_heal_tenant_orphan_resources(
    tenant: openstack_models.Tenant,
    offering_actions: dict | None = None,
) -> dict:
    """Backfill marketplace Resource rows for orphan Instance/Volume rows in tenant.

    Returns counters: healed (created) and skipped_no_offering (silent-skip case
    where create_marketplace_resource_for_imported_resources returned without
    creating because the per-tenant offering was still missing).

    ``offering_actions`` is the output of :func:`self_heal_tenant_offerings`.
    When the per-tenant offering for a resource class is in a known-broken
    state (duplicates, no parent, auto-create disabled), the per-orphan loop
    is collapsed into a single summary ERROR per class — the diagnosis is
    tenant-level, not resource-level, and the per-resource lines dominated
    the error log without adding information.

    Note: when collapsing the ``skipped_multiple`` case, orphans that would
    otherwise have hit ``MultipleObjectsReturned`` inside
    ``create_marketplace_resource_for_imported_resources`` (counted via the
    ``logger.exception`` branch) now flow into ``*_skipped_no_offering``.
    The counter label is slightly imprecise but accurately reflects the
    tenant-level diagnosis the operator must fix.
    """
    offering_actions = offering_actions or {}
    counters = {
        "instances_healed": 0,
        "volumes_healed": 0,
        "instances_skipped_no_offering": 0,
        "volumes_skipped_no_offering": 0,
    }

    for klass, healed_key, skipped_key, offering_type in (
        (
            openstack_models.Instance,
            "instances_healed",
            "instances_skipped_no_offering",
            OPENSTACK_INSTANCE_OFFERING,
        ),
        (
            openstack_models.Volume,
            "volumes_healed",
            "volumes_skipped_no_offering",
            OPENSTACK_VOLUME_OFFERING,
        ),
    ):
        content_type = ContentType.objects.get_for_model(klass)
        linked_ids = set(
            marketplace_models.Resource.objects.filter(
                content_type=content_type
            ).values_list("object_id", flat=True)
        )
        orphans_qs = klass.objects.filter(tenant=tenant).exclude(id__in=linked_ids)

        action = offering_actions.get(offering_type)
        if action in _BROKEN_OFFERING_ACTIONS:
            orphan_count = orphans_qs.count()
            counters[skipped_key] = orphan_count
            if orphan_count:
                logger.error(
                    "Self-heal could not link %d %s orphan(s) to marketplace "
                    "Resources: per-tenant offering is %s. Tenant: %s. "
                    "Operator action required.",
                    orphan_count,
                    klass.__name__,
                    action,
                    tenant.id,
                )
            continue

        for orphan in orphans_qs:
            try:
                create_marketplace_resource_for_imported_resources(orphan)
            except (ObjectDoesNotExist, MultipleObjectsReturned):
                logger.exception(
                    "Self-heal failed to create marketplace Resource for "
                    "%s %s in tenant %s",
                    klass.__name__,
                    orphan.id,
                    tenant.id,
                )
                continue
            if marketplace_models.Resource.objects.filter(scope=orphan).exists():
                counters[healed_key] += 1
            else:
                counters[skipped_key] += 1
                logger.error(
                    "Self-heal could not link %s %s to a marketplace Resource: "
                    "per-tenant offering missing. Tenant: %s",
                    klass.__name__,
                    orphan.id,
                    tenant.id,
                )

    if counters["instances_healed"] or counters["volumes_healed"]:
        logger.info(
            "Self-heal created %d instance and %d volume marketplace Resource(s) "
            "for tenant %s",
            counters["instances_healed"],
            counters["volumes_healed"],
            tenant.id,
        )

    return counters


def self_heal_tenant_marketplace_model(tenant: openstack_models.Tenant) -> dict:
    """Top-level entrypoint: heal per-tenant offerings, then orphan resources.

    Order matters: offerings must be present before orphan creation can succeed.
    """
    offering_actions = self_heal_tenant_offerings(tenant)
    orphan_counters = self_heal_tenant_orphan_resources(tenant, offering_actions)
    return {**offering_actions, **orphan_counters}


def import_instances_and_volumes_of_tenant(tenant: openstack_models.Tenant):
    backend = OpenStackBackend(tenant.service_settings)

    for instance in backend.get_importable_instances(tenant):
        created_instance = backend.import_instance(
            tenant, instance["backend_id"], tenant.project
        )
        create_marketplace_resource_for_imported_resources(created_instance)

    for volume in backend.get_importable_volumes(tenant):
        created_volume = backend.import_volume(
            tenant, volume["backend_id"], tenant.project
        )
        create_marketplace_resource_for_imported_resources(created_volume)


def terminate_expired_instances_and_volumes_of_tenant(tenant: openstack_models.Tenant):
    backend = OpenStackBackend(tenant.service_settings)

    for instance in backend.get_expired_instances(tenant):
        try:
            resource = marketplace_models.Resource.objects.get(
                project=instance.project, scope=instance
            )
            resource.set_state_terminated()
            resource.save()
        except marketplace_models.Resource.DoesNotExist:
            pass
        instance.delete()

    for volume in backend.get_expired_volumes(tenant):
        try:
            resource = marketplace_models.Resource.objects.get(
                project=volume.project, scope=volume
            )
            resource.set_state_terminated()
            resource.save()
        except marketplace_models.Resource.DoesNotExist:
            pass
        volume.delete()


def create_offerings_for_volume_and_instance(tenant: openstack_models.Tenant):
    if not settings.WALDUR_MARKETPLACE_OPENSTACK[
        "AUTOMATICALLY_CREATE_PRIVATE_OFFERING"
    ]:
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=tenant)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping offering creation for tenant because its marketplace resource "
            "does not exist. OpenStack tenant ID: %s",
            tenant.id,
        )
        return

    parent_offering = resource.offering
    if parent_offering is None:
        logger.error(
            "Skipping per-tenant offering creation: tenant marketplace resource "
            "has no parent offering. OpenStack tenant ID: %s",
            tenant.id,
        )
        return

    for offering_type in (OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING):
        category, offering_name = get_category_and_name_for_offering_type(
            offering_type, tenant
        )
        actual_customer = tenant.project.customer
        payload = dict(
            type=offering_type,
            name=offering_name,
            scope=tenant,
            shared=False,
            category=category,
            billable=False,
            parent=parent_offering,
            customer=actual_customer,
            project=tenant.project,
        )

        fields = (
            "state",
            "attributes",
            "thumbnail",
            "vendor_details",
            "getting_started",
            "integration_guide",
            "latitude",
            "longitude",
        )
        for field in fields:
            payload[field] = getattr(parent_offering, field)

        if (
            not marketplace_models.Offering.objects.filter(
                type=offering_type,
                scope=tenant,
                customer=actual_customer,
                project=tenant.project,
            )
            .exclude(state=OfferingStates.ARCHIVED)
            .exists()
        ):
            marketplace_models.Offering.objects.create(**payload)


def create_marketplace_resource_for_imported_resources(
    instance, offering=None, plan=None
):
    if marketplace_models.Resource.objects.filter(scope=instance).exists():
        logger.warning(
            "Skipping creation of marketplace resource "
            "for OpenStack instance with ID %s because it already exists.",
            instance.id,
        )
        return
    resource = marketplace_models.Resource(
        # backend_id is None if instance is being restored from backup because
        # on database level there's uniqueness constraint enforced for backend_id
        # but in marketplace resource backend_is not nullable
        backend_id=instance.backend_id or "",
        project=instance.project,
        state=get_resource_state(instance.state),
        name=instance.name,
        scope=instance,
        created=instance.created,
        plan=plan,
        offering=offering,
    )

    if isinstance(instance, openstack_models.Instance):
        offering = offering or get_offering(
            OPENSTACK_INSTANCE_OFFERING, instance.tenant
        )

        if not offering:
            return

        resource.offering = offering

        resource.init_cost()
        resource.save()
        import_instance_metadata(resource)
        update_external_addresses_of_resource(resource)

    if isinstance(instance, openstack_models.Volume):
        offering = offering or get_offering(OPENSTACK_VOLUME_OFFERING, instance.tenant)

        if not offering:
            return

        resource.offering = offering

        resource.init_cost()
        resource.save()
        import_volume_metadata(resource)

    if isinstance(instance, openstack_models.Tenant):
        offering = offering or get_offering(
            OPENSTACK_TENANT_OFFERING, instance.service_settings
        )

        if not offering:
            return

        resource.offering = offering
        resource.init_cost()
        backend = instance.get_backend()

        storage_mode = offering.plugin_options.get("storage_mode")
        limits = backend.get_tenant_limits(instance, storage_mode == STORAGE_MODE_FIXED)
        resource.limits = limits
        resource.save()
        import_resource_metadata(resource)
        create_offerings_for_volume_and_instance(instance)


def _map_ip_via_cidr(floating_ip_address, floating_cidr, public_cidr):
    """Map a floating IP to a public IP using CIDR-based translation."""
    return (
        ".".join(public_cidr.split(".")[:-1]) + "." + floating_ip_address.split(".")[-1]
    )


def get_external_ip(offering, floating_ip_address):
    ip_addr = ipaddress.ip_address(floating_ip_address)

    # Try ExternalSubnet.public_ip_range first
    if offering.scope:
        external_subnets = openstack_models.ExternalSubnet.objects.filter(
            network__settings=offering.scope,
        ).exclude(public_ip_range="")
        for subnet in external_subnets:
            if subnet.cidr and ip_addr in ipaddress.ip_network(subnet.cidr):
                return _map_ip_via_cidr(
                    floating_ip_address, subnet.cidr, subnet.public_ip_range
                )

    # Fall back to secret_options-based mapping
    ipv4_external_ip_mapping = offering.secret_options.get(
        "ipv4_external_ip_mapping", []
    )
    if not ipv4_external_ip_mapping:
        return

    for offering_external_ip in ipv4_external_ip_mapping:
        ip_network = ipaddress.ip_network(offering_external_ip["floating_ip"])

        if ip_addr in ip_network:
            return _map_ip_via_cidr(
                floating_ip_address,
                offering_external_ip["floating_ip"],
                offering_external_ip["external_ip"],
            )


def update_external_addresses_of_resource(resource: marketplace_models.Resource):
    instance = cast(openstack_models.Instance, resource.scope)

    if not instance:
        return

    floating_ips = instance.floating_ips.exclude(address__isnull=True).order_by(
        "address"
    )
    resource.backend_metadata["external_address"] = []

    for floating_ip in floating_ips:
        if not resource.offering.parent:
            continue

        external_ip = get_external_ip(
            resource.offering.parent,
            floating_ip.address,
        )

        floating_ip.external_address = external_ip
        resource.backend_metadata["external_address"].append(external_ip)

        floating_ip.save()
        resource.save()


def update_external_addresses_of_floating_ip(floating_ip: openstack_models.FloatingIP):
    if not floating_ip.address:
        if floating_ip.external_address:
            floating_ip.external_address = None
            floating_ip.save()
        return

    if not floating_ip.port or not floating_ip.port.instance:
        return

    try:
        instance = floating_ip.port.instance
        resource = marketplace_models.Resource.objects.filter(scope=instance).get()
        update_external_addresses_of_resource(resource)
    except marketplace_models.Resource.DoesNotExist:
        return


def update_external_addresses_of_offering_floating_ips(
    parent_offering: marketplace_models.Offering,
):
    offerings = marketplace_models.Offering.objects.filter(
        parent=parent_offering, type=OPENSTACK_INSTANCE_OFFERING
    )

    if not offerings:
        return

    resources = marketplace_models.Resource.objects.filter(
        offering__in=offerings,
        content_type=ContentType.objects.get_for_model(openstack_models.Instance),
    ).exclude(object_id__isnull=True)

    for resource in resources:
        update_external_addresses_of_resource(resource)


def set_ports_status_for_order(order, status):
    for port_attribute in order.attributes.get("ports", []):
        port_url = port_attribute.get("port")
        if port_url:
            port_uuid = port_url.rstrip("/").split("/")[-1]
            port = openstack_models.Port.objects.filter(uuid=port_uuid).first()
            if port:
                port.status = status
                port.save()


def delete_instance(instance, attributes=None, is_async=True):
    if not attributes:
        attributes = {}

    delete_volumes = attributes.get("delete_volumes", True)
    release_floating_ips = attributes.get("release_floating_ips", True)

    logger.info(
        "Scheduling deletion of instance %s (delete_volumes=%s, release_floating_ips=%s)",
        instance.uuid,
        delete_volumes,
        release_floating_ips,
    )

    force = instance.state == CoreStates.ERRED
    transaction.on_commit(
        lambda: openstack_executors.InstanceDeleteExecutor.execute(
            instance,
            force=force,
            delete_volumes=delete_volumes,
            release_floating_ips=release_floating_ips,
            is_async=is_async,
        )
    )


def delete_volume(volume, attributes=None, is_async=True):
    """
    Delete an OpenStack volume, bypassing viewset permission filtering.

    This function is similar to delete_instance and is used by VolumeDeleteProcessor
    to avoid permission filtering issues when deleting volumes through the marketplace.
    """
    if not attributes:
        attributes = {}

    # Validate volume state (same as MarketplaceVolumeViewSet._can_destroy_volume)
    if volume.state == CoreStates.ERRED:
        pass  # Allow deletion of errored volumes
    elif volume.state != CoreStates.OK:
        raise IncorrectStateException(_("Volume should be in OK state."))
    else:
        core_validators.RuntimeStateValidator(
            "available", "error", "error_restoring", "error_extending", ""
        )(volume)

    # Check for dependent snapshots (same as MarketplaceVolumeViewSet._volume_snapshots_exist)
    if volume.snapshots.exists():
        raise IncorrectStateException(_("Volume has dependent snapshots."))

    force = volume.state == CoreStates.ERRED
    transaction.on_commit(
        lambda: openstack_executors.VolumeDeleteExecutor.execute(
            volume,
            force=force,
            is_async=is_async,
        )
    )


# --- Placement billing reconciliation ----------------------------------------
#
# Read-only audit comparing the flavor-derived billing of an OpenStack instance
# (cores/ram/disk, which feed the tenant ComponentUsage via quota) against what
# Placement actually allocated to the same consumer. Surfaces drift — in
# particular specialty resource classes (VGPU, PCI_DEVICE, custom) that Placement
# reports but no OfferingComponent bills for, i.e. silent under-billing.


class DriftSeverity:
    # Billed amount differs from actual usage and the customer is under-billed,
    # or a resource in use has no OfferingComponent at all.
    HIGH = "HIGH"
    # Billed amount exceeds actual usage: customer over-billed. Not revenue loss.
    MEDIUM = "MEDIUM"


# Placement resource class -> marketplace component type. Both VCPU and
# MEMORY_MB come straight from the flavor, which is exactly what drives the
# tenant ComponentUsage, so they reconcile 1:1 (RAM is MiB on both sides).
PLACEMENT_CLASS_TO_COMPONENT = {
    "VCPU": CORES_TYPE,
    "MEMORY_MB": RAM_TYPE,
}

# Placement resource classes that are deliberately not reconciled. DISK_GB is
# the hypervisor-local/ephemeral disk Nova reserves; instance storage in Waldur
# is the sum of attached Cinder volumes (see backend._import_instance), a
# different subsystem Placement knows nothing about. Boot-from-volume instances
# report DISK_GB=0 against a multi-GB volume, so a quantity comparison here only
# produces false drift. Cinder storage under-billing is detected separately by
# ``detect_untracked_volume_types``.
PLACEMENT_CLASSES_IGNORED = {"DISK_GB"}


def aggregate_placement_allocations(allocations: dict | None) -> dict:
    """Sum Placement resources across all resource providers for one consumer.

    ``allocations`` is the raw shape returned by ``PlacementClient.get_allocations``::

        {"<rp_uuid>": {"resources": {"VCPU": 2, ...}, "generation": N}, ...}

    Returns a flat ``{resource_class: total}`` dict.
    """
    totals: dict[str, int] = {}
    for record in (allocations or {}).values():
        for resource_class, amount in (record or {}).get("resources", {}).items():
            totals[resource_class] = totals.get(resource_class, 0) + amount
    return totals


def placement_to_component_values(placement_resources: dict) -> tuple[dict, dict]:
    """Split aggregated Placement resources into mapped and unmapped buckets.

    ``mapped`` maps a marketplace component type to a value in that component's
    stored unit. ``unmapped`` keeps the raw resource classes Placement reports
    that have no component mapping at all (VGPU, PCI_DEVICE, custom). Ignored
    classes (see ``PLACEMENT_CLASSES_IGNORED``) appear in neither bucket.
    """
    mapped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for resource_class, amount in placement_resources.items():
        if resource_class in PLACEMENT_CLASSES_IGNORED:
            continue
        component_type = PLACEMENT_CLASS_TO_COMPONENT.get(resource_class)
        if component_type is None:
            unmapped[resource_class] = unmapped.get(resource_class, 0) + amount
            continue
        mapped[component_type] = mapped.get(component_type, 0) + amount
    return mapped, unmapped


def reconcile_instance_allocation(
    flavor_values: dict,
    placement_resources: dict,
    tracked_types: set,
    flag_untracked: bool,
) -> list[dict]:
    """Compare one instance's flavor-derived billing against Placement.

    :param flavor_values: ``{component_type: value}`` in component units
        (cores/ram only — storage is reconciled against Cinder, not Placement).
    :param placement_resources: aggregated ``{resource_class: amount}`` from
        ``aggregate_placement_allocations``.
    :param tracked_types: component types the plan bills for.
    :param flag_untracked: also report Placement resource classes with no
        matching OfferingComponent (the VGPU under-billing case).
    :return: list of drift dicts ``{resource_class, billed, actual, severity, tag}``.
    """
    drifts: list[dict] = []
    mapped, unmapped = placement_to_component_values(placement_resources)

    # Drift on components the plan tracks: billed vs Placement.
    for component_type in sorted(tracked_types):
        billed = flavor_values.get(component_type, 0)
        allocated = mapped.get(component_type, 0)
        if billed == allocated:
            continue
        under_billed = billed < allocated
        drifts.append(
            {
                "resource_class": component_type,
                "billed": billed,
                "actual": allocated,
                "severity": DriftSeverity.HIGH
                if under_billed
                else DriftSeverity.MEDIUM,
                "tag": "under-billed" if under_billed else "over-billed",
            }
        )

    # Placement resources the plan does not bill for at all.
    if flag_untracked:
        untracked = dict(unmapped)
        for component_type, amount in mapped.items():
            if component_type not in tracked_types:
                untracked[component_type] = amount
        for resource_class, amount in sorted(untracked.items()):
            if amount <= 0:
                continue
            drifts.append(
                {
                    "resource_class": resource_class,
                    "billed": 0,
                    "actual": amount,
                    "severity": DriftSeverity.HIGH,
                    "tag": "no matching OfferingComponent",
                }
            )

    return drifts


def detect_untracked_volume_types(
    volume_type_usages: dict,
    tracked_types: set,
) -> list[dict]:
    """Flag Cinder volume types in use that have no matching ``gigabytes_<type>``
    OfferingComponent — the storage twin of the VGPU under-billing case.

    Only meaningful in dynamic storage mode; in fixed mode every volume type
    rolls into the single ``storage`` component, so there is no per-type gap and
    the caller should not invoke this.

    :param volume_type_usages: ``{quota_name: gigabytes_in_use}``, typically the
        ``gigabytes_*`` entries of ``tenant.quota_usages``.
    :param tracked_types: component types the plan bills for.
    """
    drifts: list[dict] = []
    for quota_name, amount in sorted(volume_type_usages.items()):
        if not is_valid_volume_type_name(quota_name):
            continue
        if amount <= 0 or quota_name in tracked_types:
            continue
        drifts.append(
            {
                "resource_class": quota_name,
                "billed": 0,
                "actual": amount,
                "severity": DriftSeverity.HIGH,
                "tag": "no matching OfferingComponent",
            }
        )
    return drifts


# --- Placement-sourced ComponentUsage ----------------------------------------
#
# Opt-in alternative to flavor-derived billing: source compute usage straight
# from Placement allocations. Reuses the reconciliation pure functions
# (aggregate_placement_allocations, placement_to_component_values). Enabled per
# offering via plugin_options["billing_source"] == "placement".


def collect_placement_allocations(tenant) -> dict:
    """Sum Placement allocations across all of a tenant's instances.

    One ``PlacementClient`` per tenant. Instances with no ``backend_id`` or no
    Placement record contribute nothing — transient (just created) or never
    scheduled — mirroring the reconciliation audit. Returns a flat
    ``{resource_class: total}`` dict.
    """
    backend = tenant.get_backend()
    placement = get_placement_client(backend.admin_session)
    instances = (
        openstack_models.Instance.objects.filter(tenant=tenant)
        .exclude(backend_id="")
        .exclude(backend_id__isnull=True)
    )
    totals: dict[str, int] = {}
    for instance in instances:
        allocations = placement.get_allocations(instance.backend_id)
        for resource_class, amount in aggregate_placement_allocations(
            allocations
        ).items():
            totals[resource_class] = totals.get(resource_class, 0) + amount
    return totals


def merge_placement_usages(quota_usages: dict, placement_totals: dict) -> dict:
    """Overlay Placement-derived compute usage onto quota-derived usage.

    cores/ram are replaced with the Placement-aggregated values; specialty
    resource classes (VGPU, PCI_DEVICE, custom) become one component each,
    matched to an ``OfferingComponent`` by lowercased class name (``VGPU`` ->
    ``vgpu``). A class with no matching component is logged and skipped by
    ``import_current_usages``, so it is surfaced but never billed.

    Storage and every other quota-sourced component are left untouched —
    Placement ``DISK_GB`` is deliberately ignored (see
    ``PLACEMENT_CLASSES_IGNORED``): instance storage is Cinder volumes, a
    subsystem Placement does not track.
    """
    mapped, unmapped = placement_to_component_values(placement_totals)
    result = dict(quota_usages)
    result[CORES_TYPE] = mapped.get(CORES_TYPE, 0)
    result[RAM_TYPE] = mapped.get(RAM_TYPE, 0)
    for resource_class, amount in unmapped.items():
        if amount > 0:
            result[resource_class.lower()] = amount
    return result
