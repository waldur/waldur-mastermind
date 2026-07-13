import logging
from typing import cast

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.quotas.models import QuotaUsage
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import Offering, Resource
from waldur_openstack import models as openstack_models
from waldur_openstack.models import (
    FloatingIP,
    Instance,
    Network,
    Port,
    Router,
    Tenant,
    Volume,
    VolumeType,
)
from waldur_openstack.utils import volume_type_name_to_quota_name

from . import (
    STORAGE_MODE_FIXED,
    tasks,
    utils,
)

logger = logging.getLogger(__name__)


def create_offering_from_tenant(
    sender, instance: openstack_models.Tenant, created=False, **kwargs
):
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.tracker.previous("state") != CoreStates.CREATING:
        return

    if instance.state != CoreStates.OK:
        return

    utils.create_offerings_for_volume_and_instance(instance)


def archive_offering(sender, instance: Tenant, **kwargs):
    """Archive marketplace offerings when OpenStack tenant is deleted."""
    marketplace_models.Offering.objects.filter(scope=instance).update(
        state=OfferingStates.ARCHIVED
    )


def synchronize_volume_metadata_on_save(
    sender, instance: Volume, created=False, **kwargs
):
    volume = instance
    if not created and not set(volume.tracker.changed()) & {
        "size",
        "instance_id",
        "type_id",
    }:
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=volume)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack volume "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            instance.id,
        )
        return

    utils.import_volume_metadata(resource)


def synchronize_volume_metadata_on_pull(sender, instance: Volume, **kwargs):
    volume = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=volume)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack volume "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            instance.id,
        )
        return

    utils.import_volume_metadata(resource)


def synchronize_instance_hypervisor_hostname(
    sender, instance: Instance, created=False, **kwargs
):
    if created:
        return

    if not created and not set(instance.tracker.changed()) & {
        "hypervisor_hostname",
    }:
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack instance "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            instance.id,
        )
        return

    resource.backend_metadata["hypervisor_hostname"] = instance.hypervisor_hostname
    resource.save()


def synchronize_instance_name(sender, instance: Instance, created=False, **kwargs):
    if not created and not set(instance.tracker.changed()) & {"name"}:
        return

    for volume in instance.volumes.all():
        try:
            resource = marketplace_models.Resource.objects.get(scope=volume)
        except ObjectDoesNotExist:
            logger.debug(
                "Skipping resource synchronization for OpenStack volume "
                "because marketplace resource does not exist. "
                "Resource ID: %s",
                instance.id,
            )
            continue

        resource.backend_metadata["instance_name"] = volume.instance.name
        resource.save(update_fields=["backend_metadata"])


def synchronize_instance_after_pull(sender, instance: Instance, **kwargs):
    if (
        instance.tracker.has_changed("action")
        and instance.tracker.previous("action") == "Pull"
    ):
        try:
            resource = marketplace_models.Resource.objects.get(scope=instance)
            utils.import_instance_metadata(resource)
        except ObjectDoesNotExist:
            pass


def synchronize_directly_connected_ips(
    sender, instance: Instance, created=False, **kwargs
):
    if not created and not set(instance.tracker.changed()) & {
        "directly_connected_ips",
    }:
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack instance "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            instance,
        )
        return

    utils.import_instance_metadata(resource)


def synchronize_ports(sender, instance: Port, created=False, **kwargs):
    port = instance
    if not created and not set(port.tracker.changed()) & {
        "fixed_ips",
        "instance_id",
    }:
        return

    vms = {vm for vm in (port.instance_id, port.tracker.previous("instance_id")) if vm}

    for vm in vms:
        try:
            scope = openstack_models.Instance.objects.get(id=vm)
            resource = marketplace_models.Resource.objects.get(scope=scope)
        except ObjectDoesNotExist:
            logger.debug(
                "Skipping resource synchronization for OpenStack instance "
                "because marketplace resource does not exist. "
                "Resource ID: %s",
                vm,
            )
            continue

        utils.import_instance_metadata(resource)


def synchronize_floating_ips(
    sender, instance: openstack_models.FloatingIP, created=False, **kwargs
):
    floating_ip = instance
    if not created and not set(instance.tracker.changed()) & {
        "address",
        "port_id",
    }:
        return

    ports = {
        ip
        for ip in (
            floating_ip.port_id,
            floating_ip.tracker.previous("port_id"),
        )
        if ip
    }
    for ip_id in ports:
        try:
            scope = openstack_models.Instance.objects.get(ports__id=ip_id)
            resource = marketplace_models.Resource.objects.get(scope=scope)
        except ObjectDoesNotExist:
            logger.debug(
                "Skipping resource synchronization for OpenStack instance "
                "because marketplace resource does not exist. "
                "Resource ID: %s",
                ip_id,
            )
            continue

        utils.import_instance_metadata(resource)


def import_instance_metadata(vm):
    if not vm:
        return
    try:
        resource = marketplace_models.Resource.objects.get(scope=vm)
    # AttributeError can be raised by generic foreign key, WAL-2662
    except (ObjectDoesNotExist, AttributeError):
        logger.debug(
            "Skipping resource synchronization for OpenStack instance "
            "because marketplace resource does not exist. "
            "Virtual machine ID: %s",
            vm.id,
        )
    else:
        utils.import_instance_metadata(resource)


def synchronize_ports_on_delete(sender, instance: Port, **kwargs):
    try:
        vm = instance.instance
    except ObjectDoesNotExist:
        pass
    else:
        import_instance_metadata(vm)


def synchronize_floating_ips_on_delete(sender, instance: FloatingIP, **kwargs):
    if instance.port:
        import_instance_metadata(instance.port.instance)


def create_resource_of_volume_if_instance_created(
    sender, instance: Resource, created=False, **kwargs
):
    resource = instance

    if not resource.scope or not getattr(resource.offering, "scope", None):
        return

    if resource.offering.type != OPENSTACK_INSTANCE_OFFERING:
        return

    vm = cast(Instance, resource.scope)

    volume_offering = utils.get_offering(
        OPENSTACK_VOLUME_OFFERING, getattr(resource.offering, "scope", None)
    )
    if not volume_offering:
        return

    for volume in vm.volumes.all():
        if marketplace_models.Resource.objects.filter(scope=volume).exists():
            continue

        volume_resource = marketplace_models.Resource(
            project=resource.project,
            offering=volume_offering,
            created=resource.created,
            name=volume.name,
            scope=volume,
        )

        volume_resource.init_cost()
        volume_resource.save()
        utils.import_volume_metadata(volume_resource)


def create_marketplace_resource_for_imported_resources(
    sender, instance: Instance, offering=None, plan=None, **kwargs
):
    utils.create_marketplace_resource_for_imported_resources(instance, offering, plan)


def import_resource_metadata_when_resource_is_created(
    sender, instance: marketplace_models.Resource, created=False, **kwargs
):
    """Import OpenStack resource metadata when marketplace resource is created."""
    if not created:
        return

    #  If the resource has just been created and the save_base method has not yet completed,
    #  then the resource.tracker.__exit__ method has not yet been called
    #  and the tracker includes the fields set when the resource was created.
    instance.tracker.set_saved_fields(fields=instance.tracker.changed())

    if isinstance(instance.scope, openstack_models.Volume):
        utils.import_volume_metadata(instance)

    if isinstance(instance.scope, openstack_models.Instance):
        utils.import_instance_metadata(instance)


def update_openstack_tenant_usages(
    sender, instance: QuotaUsage, created=False, **kwargs
):
    if not created:
        return

    if not isinstance(instance.scope, openstack_models.Tenant):
        return

    tenant = instance.scope

    try:
        resource = marketplace_models.Resource.objects.get(scope=tenant)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping usages synchronization for tenant because "
            "resource does not exist. OpenStack tenant ID: %s",
            tenant.id,
        )
        return

    transaction.on_commit(lambda: utils.import_usage(resource))


def import_usage_on_tenant_quotas_pulled(
    sender, instance: openstack_models.Tenant, **kwargs
):
    tenant = instance
    try:
        resource = marketplace_models.Resource.objects.get(scope=tenant)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping usages synchronization for tenant because "
            "resource does not exist. OpenStack tenant ID: %s",
            tenant.id,
        )
        return

    utils.import_usage(resource)


def create_offering_component_for_volume_type(
    sender, instance: VolumeType, created=False, **kwargs
):
    """Create marketplace offering component when OpenStack volume type is created."""
    volume_type = instance

    try:
        offering = marketplace_models.Offering.objects.get(scope=volume_type.settings)
    except marketplace_models.Offering.DoesNotExist:
        logger.warning(
            "Skipping synchronization of volume type with "
            "marketplace because offering for service settings is not have found. "
            "Settings ID: %s",
            volume_type.settings.id,
        )
        return

    storage_mode = offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
    if storage_mode == STORAGE_MODE_FIXED:
        logger.debug(
            "Skipping synchronization of volume type with "
            "marketplace because offering has fixed storage mode enabled. "
            "Offering ID: %s",
            offering.id,
        )
        return

    content_type = ContentType.objects.get_for_model(volume_type)

    # It is assumed that article code and product code are filled manually via UI
    marketplace_models.OfferingComponent.objects.update_or_create(
        object_id=volume_type.id,
        content_type=content_type,
        defaults=dict(
            offering=offering,
            name="Storage (%s)" % volume_type.name,
            # It is expected that internal name of offering component related to volume type
            # matches storage quota name generated in OpenStack
            type=volume_type_name_to_quota_name(volume_type.name),
            measured_unit="GB",
            description=volume_type.description,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        ),
    )


def delete_offering_component_for_volume_type(sender, instance: VolumeType, **kwargs):
    marketplace_models.OfferingComponent.objects.filter(scope=instance).delete()


def synchronize_limits_when_storage_mode_is_switched(
    sender, instance: Offering, created=False, **kwargs
):
    offering = instance

    if created:
        return

    if offering.type != OPENSTACK_TENANT_OFFERING:
        return

    if not offering.tracker.has_changed("plugin_options"):
        return

    old_mode = (offering.tracker.previous("plugin_options") or {}).get("storage_mode")
    new_mode = (offering.plugin_options or {}).get("storage_mode")

    if not new_mode:
        logger.debug(
            "Skipping OpenStack tenant synchronization because new storage mode is not defined"
        )
        return

    if old_mode == new_mode:
        logger.debug(
            "Skipping OpenStack tenant synchronization because storage mode is not changed."
        )
        return

    logger.info(
        "Synchronizing OpenStack tenant limits because storage mode is switched "
        "from %s to %s",
        old_mode,
        new_mode,
    )

    resources = marketplace_models.Resource.objects.filter(offering=offering).exclude(
        state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING)
    )

    for resource in resources:
        utils.import_limits_when_storage_mode_is_switched(resource)
        utils.import_usage(resource)

        serialized_resource = core_utils.serialize_instance(resource)
        transaction.on_commit(
            lambda: tasks.push_tenant_limits.delay(serialized_resource)
        )


def import_instances_and_volumes_if_tenant_has_been_imported(
    sender, instance: Tenant, *args, **kwargs
):
    tenant = instance

    # Ensure default categories exist before importing
    utils.get_offering_category_for_instance()
    utils.get_offering_category_for_volume()

    serialized_resource = core_utils.serialize_instance(tenant)
    transaction.on_commit(
        lambda: tasks.sync_instances_and_volumes_of_tenant.delay(serialized_resource)
    )


def synchronize_tenant_name(
    sender, instance: Tenant, offering=None, plan=None, **kwargs
):
    tenant = instance

    if not instance.tracker.has_changed("name"):
        return

    offerings = marketplace_models.Offering.objects.filter(
        object_id=tenant.id, content_type=ContentType.objects.get_for_model(tenant)
    )

    for offering in offerings.filter(type=OPENSTACK_INSTANCE_OFFERING):
        offering.name = utils.get_offering_name_for_instance(tenant)
        offering.save(update_fields=["name"])

    for offering in offerings.filter(type=OPENSTACK_VOLUME_OFFERING):
        offering.name = utils.get_offering_name_for_volume(tenant)
        offering.save(update_fields=["name"])


def synchronize_router_backend_metadata(
    sender, instance: Router, created=False, **kwargs
):
    router = instance

    if not created and not set(router.tracker.changed()) & {
        "fixed_ips",
    }:
        return

    try:
        resource: marketplace_models.Resource = marketplace_models.Resource.objects.get(
            scope=router.tenant
        )
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack router "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            router.id,
        )
        return

    metadata_router_ips = set(
        resource.backend_metadata.get("routers", {}).get(router.uuid.hex, [])
    )
    metadata_all_ips = set(resource.backend_metadata.get("router_fixed_ips", []))
    new_ips = (metadata_all_ips - metadata_router_ips) | set(router.fixed_ips)
    resource.backend_metadata["router_fixed_ips"] = list(new_ips)

    if "routers" in resource.backend_metadata.keys():
        resource.backend_metadata["routers"][router.uuid.hex] = router.fixed_ips
    else:
        resource.backend_metadata["routers"] = {}
        resource.backend_metadata["routers"][router.uuid.hex] = router.fixed_ips

    resource.save()


def tenant_does_not_exist_in_backend(sender, instance: Tenant, created=False, **kwargs):
    tenant = instance

    for resource in marketplace_models.Resource.objects.filter(scope=tenant):
        marketplace_utils.terminate_resource(
            resource, core_utils.get_system_robot(), "Does not exist in backend."
        )


def set_mtu_when_network_has_been_created(
    sender, instance: Network, created=False, **kwargs
):
    if not created:
        return

    network = instance
    # this won't catch the situation when network is created on tenant creation
    resource = marketplace_models.Resource.objects.filter(scope=network.tenant).first()
    if not resource:
        return

    mtu = resource.offering.plugin_options.get("default_internal_network_mtu")

    if mtu:
        network.mtu = mtu
        network.save()


def update_floating_ip_external_addresses(
    sender, instance: FloatingIP, created=False, **kwargs
):
    # Recompute when the address OR the port association changes. A floating IP
    # is often attached to an instance port in a later save that leaves the
    # address untouched, so triggering on address alone leaves external_address
    # (the 1:1 NAT public IP) unset and the VM looks like it has no public IP.
    if not (
        instance.tracker.has_changed("address")
        or instance.tracker.has_changed("port_id")
        or (created and instance.address)
    ):
        return

    utils.update_external_addresses_of_floating_ip(instance)


def update_instances_ip_external_addresses(
    sender, instance: Offering, created=False, **kwargs
):
    offering = instance

    if offering.type != OPENSTACK_TENANT_OFFERING:
        return

    if not created and not offering.tracker.has_changed("secret_options"):
        return

    previous_secret_options = offering.tracker.previous("secret_options") or {}

    if "ipv4_external_ip_mapping" not in offering.secret_options.keys() and (
        not previous_secret_options
        or "ipv4_external_ip_mapping" not in previous_secret_options.keys()
    ):
        return

    if previous_secret_options.get(
        "ipv4_external_ip_mapping", []
    ) == offering.secret_options.get("ipv4_external_ip_mapping", []):
        return

    utils.update_external_addresses_of_offering_floating_ips(offering)


def handle_openstack_tenant_order_creation(
    sender, instance: marketplace_models.Order, created, **kwargs
):
    order = instance

    if (
        created
        and order.type == OrderTypes.CREATE
        and order.offering.type == OPENSTACK_INSTANCE_OFFERING
    ):
        utils.set_ports_status_for_order(order, "BOOKED")


def handle_openstack_tenant_order_termination(
    sender, instance: marketplace_models.Order, created, **kwargs
):
    order = instance

    if created:
        return

    if (
        order.offering.type == OPENSTACK_INSTANCE_OFFERING
        and order.tracker.has_changed("state")
        and order.state
        in (
            marketplace_models.OrderStates.ERRED,
            marketplace_models.OrderStates.CANCELED,
            marketplace_models.OrderStates.REJECTED,
        )
    ):
        utils.set_ports_status_for_order(order, "DOWN")


def synchronize_volume_metadata_on_resource_post_save(
    sender, instance: marketplace_models.Resource, created=False, **kwargs
):
    if created:
        return

    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "object_id" not in update_fields:
        return

    if not instance.tracker.has_changed("object_id"):
        return

    if isinstance(instance.scope, openstack_models.Volume):
        utils.import_volume_metadata(instance)


def populate_volume_metadata_on_resource_creation(
    sender, instance: marketplace_models.Resource, created=False, **kwargs
):
    if not created:
        return

    if instance.offering.type != OPENSTACK_VOLUME_OFFERING:
        return

    size = instance.attributes.get("size")
    if not size:
        return

    instance.backend_metadata["size"] = size
    instance.save(update_fields=["backend_metadata"])
