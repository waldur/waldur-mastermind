import ipaddress
import logging
from typing import cast

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers

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
from waldur_openstack.backend import OpenStackBackend
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


def import_usage(resource: marketplace_models.Resource):
    tenant = cast(openstack_models.Tenant, resource.scope)

    if not tenant:
        return

    usages = import_quotas(resource.offering, tenant.quota_usages)
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
                "Self-heal skipped: multiple per-tenant %s offerings exist for "
                "tenant %s (offering IDs: %s)",
                offering_type,
                tenant.id,
                [o.id for o in offerings],
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


def self_heal_tenant_orphan_resources(tenant: openstack_models.Tenant) -> dict:
    """Backfill marketplace Resource rows for orphan Instance/Volume rows in tenant.

    Returns counters: healed (created) and skipped_no_offering (silent-skip case
    where create_marketplace_resource_for_imported_resources returned without
    creating because the per-tenant offering was still missing).
    """
    counters = {
        "instances_healed": 0,
        "volumes_healed": 0,
        "instances_skipped_no_offering": 0,
        "volumes_skipped_no_offering": 0,
    }

    for klass, healed_key, skipped_key in (
        (
            openstack_models.Instance,
            "instances_healed",
            "instances_skipped_no_offering",
        ),
        (openstack_models.Volume, "volumes_healed", "volumes_skipped_no_offering"),
    ):
        content_type = ContentType.objects.get_for_model(klass)
        linked_ids = set(
            marketplace_models.Resource.objects.filter(
                content_type=content_type
            ).values_list("object_id", flat=True)
        )
        orphans = klass.objects.filter(tenant=tenant).exclude(id__in=linked_ids)
        for orphan in orphans:
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
    orphan_counters = self_heal_tenant_orphan_resources(tenant)
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
            floating_ip.external_address = []
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

    if (
        delete_volumes
        and openstack_models.Snapshot.objects.filter(
            source_volume__instance=instance
        ).exists()
    ):
        raise serializers.ValidationError(
            _("Cannot delete instance. One of its volumes has attached snapshot.")
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
