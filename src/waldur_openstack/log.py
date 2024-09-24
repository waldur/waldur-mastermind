from waldur_core.logging.loggers import EventLogger, event_logger
from waldur_core.structure import models as structure_models
from waldur_openstack import models as openstack_models
from waldur_openstack.models import FloatingIP


class TenantQuotaLogger(EventLogger):
    quota_name = str
    tenant = "openstack.Tenant"
    limit = int
    old_limit = int

    class Meta:
        event_types = ("openstack_tenant_quota_limit_updated",)
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        tenant = event_context["tenant"]
        project = tenant.project
        return {tenant, project, project.customer}


class RouterLogger(EventLogger):
    router = "openstack.Router"
    old_routes = list
    new_routes = list
    tenant_backend_id = str

    class Meta:
        event_types = ("openstack_router_updated",)
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        router = event_context["router"]
        project = router.project
        return {project, project.customer}


class SecurityGroupLogger(EventLogger):
    security_group = "openstack.SecurityGroup"

    class Meta:
        event_types = (
            "openstack_security_group_imported",
            "openstack_security_group_created",
            "openstack_security_group_updated",
            "openstack_security_group_pulled",
            "openstack_security_group_deleted",
            "openstack_security_group_cleaned",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        security_group = event_context["security_group"]
        return {
            security_group,
            security_group.tenant,
        }


class SecurityGroupRuleLogger(EventLogger):
    security_group_rule = "openstack.SecurityGroupRule"

    class Meta:
        event_types = (
            "openstack_security_group_rule_imported",
            "openstack_security_group_rule_created",
            "openstack_security_group_rule_updated",
            "openstack_security_group_rule_deleted",
            "openstack_security_group_rule_cleaned",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        security_group_rule = event_context["security_group_rule"]
        return [
            security_group_rule,
            security_group_rule.security_group,
        ]


class ServerGroupLogger(EventLogger):
    server_group = "openstack.ServerGroup"

    class Meta:
        event_types = (
            "openstack_server_group_imported",
            "openstack_server_group_pulled",
            "openstack_server_group_cleaned",
            "openstack_server_group_created",
            "openstack_server_group_deleted",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        server_group = event_context["server_group"]
        return {
            server_group,
            server_group.tenant,
        }


class NetworkLogger(EventLogger):
    network = "openstack.Network"

    class Meta:
        event_types = (
            "openstack_network_imported",
            "openstack_network_created",
            "openstack_network_updated",
            "openstack_network_pulled",
            "openstack_network_deleted",
            "openstack_network_cleaned",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        network = event_context["network"]
        return {
            network,
            network.tenant,
        }


class SubNetLogger(EventLogger):
    subnet = "openstack.SubNet"

    class Meta:
        event_types = (
            "openstack_subnet_created",
            "openstack_subnet_imported",
            "openstack_subnet_updated",
            "openstack_subnet_pulled",
            "openstack_subnet_deleted",
            "openstack_subnet_cleaned",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        subnet = event_context["subnet"]
        return {
            subnet,
            subnet.network,
        }


class PortLogger(EventLogger):
    port = "openstack.Port"

    class Meta:
        event_types = (
            "openstack_port_created",
            "openstack_port_imported",
            "openstack_port_pulled",
            "openstack_port_deleted",
            "openstack_port_cleaned",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        port = event_context["port"]
        return {
            port,
            port.network,
        }


class FloatingIPLogger(EventLogger):
    floating_ip = "openstack.FloatingIP"

    class Meta:
        event_types = (
            "openstack_floating_ip_attached",
            "openstack_floating_ip_detached",
            "openstack_floating_ip_description_updated",
        )
        event_groups = {
            "resources": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        floating_ip = event_context["floating_ip"]
        port = event_context.get("port")
        return {floating_ip, floating_ip.tenant, port}


class ResourceActionEventLogger(EventLogger):
    resource = structure_models.BaseResource
    action_details = dict

    class Meta:
        event_types = (
            "resource_pull_scheduled",
            "resource_pull_succeeded",
            "resource_pull_failed",
            # volume
            "resource_attach_scheduled",
            "resource_attach_succeeded",
            "resource_attach_failed",
            "resource_detach_scheduled",
            "resource_detach_succeeded",
            "resource_detach_failed",
            "resource_extend_scheduled",
            "resource_extend_succeeded",
            "resource_extend_failed",
            # instance
            "resource_update_security_groups_scheduled",
            "resource_update_security_groups_succeeded",
            "resource_update_security_groups_failed",
            "resource_change_flavor_scheduled",
            "resource_change_flavor_succeeded",
            "resource_change_flavor_failed",
            "resource_assign_floating_ip_scheduled",
            "resource_assign_floating_ip_succeeded",
            "resource_assign_floating_ip_failed",
            "resource_stop_scheduled",
            "resource_stop_succeeded",
            "resource_stop_failed",
            "resource_start_scheduled",
            "resource_start_succeeded",
            "resource_start_failed",
            "resource_restart_scheduled",
            "resource_restart_succeeded",
            "resource_restart_failed",
            "resource_extend_volume_scheduled",
            "resource_extend_volume_succeeded",
            "resource_extend_volume_failed",
            "resource_retype_scheduled",
            "resource_retype_succeeded",
            "resource_retype_failed",
            "resource_unassign_floating_ip_scheduled",
            "resource_unassign_floating_ip_succeeded",
            "resource_unassign_floating_ip_failed",
            "resource_update_ports_scheduled",
            "resource_update_ports_succeeded",
            "resource_update_ports_failed",
            "resource_update_allowed_address_pairs_scheduled",
            "resource_update_allowed_address_pairs_succeeded",
            "resource_update_allowed_address_pairs_failed",
            "resource_update_floating_ips_scheduled",
            "resource_update_floating_ips_succeeded",
            "resource_update_floating_ips_failed",
        )
        event_groups = {"resources": event_types}

    @staticmethod
    def get_scopes(event_context):
        resource = event_context["resource"]
        project = resource.project
        return {resource, project, project.customer}


class BackupScheduleEventLogger(EventLogger):
    resource = openstack_models.Instance
    backup_schedule = openstack_models.BackupSchedule

    class Meta:
        event_types = (
            "resource_backup_schedule_created",
            "resource_backup_schedule_deleted",
            "resource_backup_schedule_activated",
            "resource_backup_schedule_deactivated",
            "resource_backup_schedule_cleaned_up",
        )
        event_groups = {"resources": event_types}

    @staticmethod
    def get_scopes(event_context):
        return ResourceActionEventLogger.get_scopes(event_context)


class SnapshotScheduleEventLogger(EventLogger):
    resource = openstack_models.Volume
    snapshot_schedule = openstack_models.SnapshotSchedule

    class Meta:
        event_types = (
            "resource_snapshot_schedule_created",
            "resource_snapshot_schedule_deleted",
            "resource_snapshot_schedule_activated",
            "resource_snapshot_schedule_deactivated",
            "resource_snapshot_schedule_cleaned_up",
        )
        event_groups = {"resources": event_types}

    @staticmethod
    def get_scopes(event_context):
        return ResourceActionEventLogger.get_scopes(event_context)


class BackupEventLogger(EventLogger):
    resource = openstack_models.Instance

    class Meta:
        event_types = (
            "resource_backup_creation_scheduled",
            "resource_backup_creation_succeeded",
            "resource_backup_creation_failed",
            "resource_backup_restoration_scheduled",
            "resource_backup_restoration_succeeded",
            "resource_backup_restoration_failed",
            "resource_backup_deletion_scheduled",
            "resource_backup_deletion_succeeded",
            "resource_backup_deletion_failed",
            "resource_backup_schedule_creation_succeeded",
            "resource_backup_schedule_update_succeeded",
            "resource_backup_schedule_deletion_succeeded",
            "resource_backup_schedule_activated",
            "resource_backup_schedule_deactivated",
        )

    @staticmethod
    def get_scopes(event_context):
        return ResourceActionEventLogger.get_scopes(event_context)


class FloatingIPEventLogger(EventLogger):
    floating_ip = FloatingIP
    instance = openstack_models.Instance

    class Meta:
        event_types = (
            "openstack_floating_ip_connected",
            "openstack_floating_ip_disconnected",
        )
        event_groups = {"resources": event_types}

    @staticmethod
    def get_scopes(event_context):
        floating_ip = event_context["floating_ip"]
        instance = event_context["instance"]
        return [
            floating_ip,
            instance,
        ]


event_logger.register("openstack_tenant_quota", TenantQuotaLogger)
event_logger.register("openstack_router", RouterLogger)
event_logger.register("openstack_network", NetworkLogger)
event_logger.register("openstack_subnet", SubNetLogger)
event_logger.register("openstack_security_group", SecurityGroupLogger)
event_logger.register("openstack_security_group_rule", SecurityGroupRuleLogger)
event_logger.register("openstack_server_group", ServerGroupLogger)
event_logger.register("openstack_port", PortLogger)
event_logger.register("openstack_floating_ip", FloatingIPLogger)
event_logger.register("openstack_resource_action", ResourceActionEventLogger)
event_logger.register("openstack_backup_schedule", BackupScheduleEventLogger)
event_logger.register("openstack_snapshot_schedule", SnapshotScheduleEventLogger)
event_logger.register("openstack_backup", BackupEventLogger)
event_logger.register("openstack_tenant_floating_ip", FloatingIPEventLogger)
