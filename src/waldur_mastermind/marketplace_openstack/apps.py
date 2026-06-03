from django.apps import AppConfig
from django.conf import settings
from django.db.models import signals

from ..marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
)


def get_secret_attributes():
    if not settings.WALDUR_OPENSTACK["TENANT_CREDENTIALS_VISIBLE"]:
        return "user_username", "user_password"


def components_filter(offering, qs):
    # These imports must stay inside the function:
    # - waldur_openstack.utils imports models which require app registry
    # - same-package import would cause circular import at module level
    from waldur_openstack.utils import is_valid_volume_type_name

    from . import AVAILABLE_LIMITS, STORAGE_MODE_FIXED, STORAGE_TYPE

    storage_mode = offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
    if storage_mode == STORAGE_MODE_FIXED:
        # Include builtin infrastructure limits (cores, ram, storage)
        # Exclude volume type components (gigabytes_*) — they're for dynamic mode
        # Include any custom provider-added components
        all_types = set(qs.values_list("type", flat=True))
        allowed_types = set(AVAILABLE_LIMITS)
        for t in all_types:
            if t not in allowed_types and not is_valid_volume_type_name(t):
                allowed_types.add(t)
        qs = qs.filter(type__in=allowed_types)
    else:
        # Dynamic storage mode: exclude fixed "storage" component,
        # keep volume types (gigabytes_*) and custom components
        qs = qs.exclude(type=STORAGE_TYPE)
    return qs


class MarketplaceOpenStackConfig(AppConfig):
    name = "waldur_mastermind.marketplace_openstack"
    verbose_name = "Marketplace OpenStack"

    def ready(self):
        from waldur_core.quotas import models as quota_models
        from waldur_core.structure import signals as structure_signals
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_openstack.const import TENANT_COMPONENTS
        from waldur_openstack import executors as openstack_executors
        from waldur_openstack import models as openstack_models
        from waldur_openstack import signals as openstack_signals
        from waldur_openstack.apps import OpenStackConfig

        from . import (
            AVAILABLE_LIMITS,
            handlers,
            processors,
            utils,
        )

        signals.post_save.connect(
            handlers.create_offering_from_tenant,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.marketplace_openstack.create_offering_from_tenant",
        )

        signals.pre_delete.connect(
            handlers.archive_offering,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.marketplace_openstack.archive_offering",
        )

        resource_models = (
            openstack_models.Instance,
            openstack_models.Volume,
            openstack_models.Tenant,
        )
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)
        marketplace_handlers.connect_resource_handlers(*resource_models)

        manager.register(
            offering_type=OPENSTACK_TENANT_OFFERING,
            create_resource_processor=processors.TenantCreateProcessor,
            update_resource_processor=processors.TenantUpdateProcessor,
            delete_resource_processor=processors.TenantDeleteProcessor,
            components=TENANT_COMPONENTS,
            service_type=OpenStackConfig.service_name,
            secret_attributes=get_secret_attributes,
            components_filter=components_filter,
            available_limits=AVAILABLE_LIMITS,
            can_update_limits=True,
            limits_validator=utils.tenant_limits_validator,
            get_importable_resources_backend_method="get_importable_tenants",
            import_resource_backend_method="import_tenant",
            import_resource_executor=openstack_executors.TenantImportExecutor,
            pull_resource_executor=openstack_executors.TenantPullExecutor,
            is_interruptible=True,
        )

        manager.register(
            offering_type=OPENSTACK_INSTANCE_OFFERING,
            create_resource_processor=processors.InstanceCreateProcessor,
            delete_resource_processor=processors.InstanceDeleteProcessor,
            get_importable_resources_backend_method="get_importable_instances",
            import_resource_backend_method="import_instance",
            import_resource_executor=openstack_executors.InstancePullExecutor,
            pull_resource_executor=openstack_executors.InstancePullExecutor,
            is_interruptible=True,
        )

        manager.register(
            offering_type=OPENSTACK_VOLUME_OFFERING,
            create_resource_processor=processors.VolumeCreateProcessor,
            delete_resource_processor=processors.VolumeDeleteProcessor,
            get_importable_resources_backend_method="get_importable_volumes",
            import_resource_backend_method="import_volume",
            is_interruptible=True,
            pull_resource_executor=openstack_executors.VolumePullExecutor,
        )

        signals.post_save.connect(
            handlers.synchronize_volume_metadata_on_save,
            sender=openstack_models.Volume,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_volume_metadata_on_save",
        )

        structure_signals.resource_pulled.connect(
            handlers.synchronize_volume_metadata_on_pull,
            sender=openstack_models.Volume,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_volume_metadata_on_pull",
        )

        signals.post_save.connect(
            handlers.synchronize_instance_hypervisor_hostname,
            sender=openstack_models.Instance,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_instance_hypervisor_hostname",
        )

        signals.post_save.connect(
            handlers.synchronize_tenant_name,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_tenant_name",
        )

        signals.post_save.connect(
            handlers.synchronize_instance_name,
            sender=openstack_models.Instance,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_instance_name",
        )

        signals.post_save.connect(
            handlers.synchronize_instance_after_pull,
            sender=openstack_models.Instance,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_instance_after_pull",
        )

        signals.post_save.connect(
            handlers.synchronize_ports,
            sender=openstack_models.Port,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_ports",
        )

        signals.post_save.connect(
            handlers.synchronize_floating_ips,
            sender=openstack_models.FloatingIP,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_floating_ips",
        )

        signals.post_delete.connect(
            handlers.synchronize_ports_on_delete,
            sender=openstack_models.Port,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_ports_on_delete",
        )

        signals.post_delete.connect(
            handlers.synchronize_floating_ips_on_delete,
            sender=openstack_models.FloatingIP,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_floating_ips_on_delete",
        )

        signals.post_save.connect(
            handlers.synchronize_directly_connected_ips,
            sender=openstack_models.Instance,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_synchronize_directly_connected_ips",
        )

        signals.post_save.connect(
            handlers.create_resource_of_volume_if_instance_created,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "create_resource_of_volume_if_instance_created",
        )

        for model in [
            openstack_models.Instance,
            openstack_models.Volume,
            openstack_models.Tenant,
        ]:
            structure_signals.resource_imported.connect(
                handlers.create_marketplace_resource_for_imported_resources,
                sender=model,
                dispatch_uid="waldur_mastermind.marketplace_openstack."
                "create_resource_for_imported_%s" % model,
            )

        signals.post_save.connect(
            handlers.import_resource_metadata_when_resource_is_created,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "import_resource_metadata_when_resource_is_created",
        )

        signals.post_save.connect(
            handlers.update_openstack_tenant_usages,
            sender=quota_models.QuotaUsage,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "update_openstack_tenant_usages",
        )

        signals.post_save.connect(
            handlers.create_offering_component_for_volume_type,
            sender=openstack_models.VolumeType,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "create_offering_component_for_volume_type",
        )

        signals.post_delete.connect(
            handlers.delete_offering_component_for_volume_type,
            sender=openstack_models.VolumeType,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "delete_offering_component_for_volume_type",
        )

        signals.post_save.connect(
            handlers.synchronize_limits_when_storage_mode_is_switched,
            sender=marketplace_models.Offering,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "synchronize_limits_when_storage_mode_is_switched",
        )

        structure_signals.resource_imported.connect(
            handlers.import_instances_and_volumes_if_tenant_has_been_imported,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "import_instances_and_volumes_if_tenant_has_been_imported",
        )

        openstack_signals.tenant_pull_succeeded.connect(
            handlers.import_instances_and_volumes_if_tenant_has_been_imported,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "import_instances_and_volumes_if_tenant_has_been_imported_if_tenant_has_been_pulled",
        )

        openstack_signals.tenant_quotas_pulled.connect(
            handlers.import_usage_on_tenant_quotas_pulled,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "import_usage_on_tenant_quotas_pulled",
        )

        signals.post_save.connect(
            handlers.synchronize_router_backend_metadata,
            sender=openstack_models.Router,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "synchronize_router_backend_metadata",
        )

        openstack_signals.tenant_does_not_exist_in_backend.connect(
            handlers.tenant_does_not_exist_in_backend,
            sender=openstack_models.Tenant,
            dispatch_uid="waldur_mastermind.tenant_does_not_exist_in_backend."
            "tenant_does_not_exist_in_backend",
        )

        signals.post_save.connect(
            handlers.set_mtu_when_network_has_been_created,
            sender=openstack_models.Network,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "set_mtu_when_network_has_been_created",
        )

        signals.post_save.connect(
            handlers.update_floating_ip_external_addresses,
            sender=openstack_models.FloatingIP,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "update_floating_ip_external_addresses",
        )

        signals.post_save.connect(
            handlers.update_instances_ip_external_addresses,
            sender=marketplace_models.Offering,
            dispatch_uid="waldur_mastermind.marketplace_openstack."
            "update_instances_ip_external_addresses",
        )

        signals.post_save.connect(
            handlers.handle_openstack_tenant_order_creation,
            sender=marketplace_models.Order,
            dispatch_uid="waldur_mastermind.marketplace_openstack.handle_openstack_tenant_order_creation",
        )

        signals.post_save.connect(
            handlers.handle_openstack_tenant_order_termination,
            sender=marketplace_models.Order,
            dispatch_uid="waldur_mastermind.marketplace_openstack.handle_openstack_tenant_order_termination",
        )

        signals.post_save.connect(
            handlers.synchronize_volume_metadata_on_resource_post_save,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_openstack.synchronize_volume_metadata_on_resource_post_save",
        )

        signals.post_save.connect(
            handlers.populate_volume_metadata_on_resource_creation,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_openstack.populate_volume_metadata_on_resource_creation",
        )
