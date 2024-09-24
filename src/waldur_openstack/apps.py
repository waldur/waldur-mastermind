from django.apps import AppConfig
from django.db.models import signals


class OpenStackConfig(AppConfig):
    """OpenStack is a toolkit for building private and public clouds.
    This application adds support for managing OpenStack deployments -
    tenants, instances, volumes, snapshots, security groups and networks.
    """

    name = "waldur_openstack"
    label = "openstack"
    verbose_name = "OpenStack"
    service_name = "OpenStack"

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas.fields import QuotaField, TotalQuotaField
        from waldur_core.quotas.models import QuotaLimit
        from waldur_core.structure import models as structure_models
        from waldur_core.structure.models import Customer, Project
        from waldur_core.structure.registry import SupportedServices

        from . import handlers

        Network = self.get_model("Network")
        SubNet = self.get_model("SubNet")
        SecurityGroup = self.get_model("SecurityGroup")
        SecurityGroupRule = self.get_model("SecurityGroupRule")
        ServerGroup = self.get_model("ServerGroup")
        Instance = self.get_model("Instance")
        Volume = self.get_model("Volume")
        Snapshot = self.get_model("Snapshot")
        BackupSchedule = self.get_model("BackupSchedule")
        SnapshotSchedule = self.get_model("SnapshotSchedule")

        # structure
        from .backend import OpenStackBackend

        SupportedServices.register_backend(OpenStackBackend)

        from . import quotas

        quotas.inject_tenant_quotas()

        for resource in ("vcpu", "ram", "storage"):
            structure_models.ServiceSettings.add_quota_field(
                name="openstack_%s" % resource,
                quota_field=QuotaField(
                    creation_condition=lambda service_settings: service_settings.type
                    == OpenStackConfig.service_name
                ),
            )

        permission_signals.role_revoked.connect(
            handlers.remove_ssh_key_from_tenants,
            dispatch_uid="openstack.handlers.remove_ssh_key_from_tenants",
        )

        signals.pre_delete.connect(
            handlers.remove_ssh_key_from_all_tenants_on_it_deletion,
            sender=core_models.SshPublicKey,
            dispatch_uid="openstack.handlers.remove_ssh_key_from_all_tenants_on_it_deletion",
        )

        signals.post_save.connect(
            handlers.log_tenant_quota_update,
            sender=QuotaLimit,
            dispatch_uid="openstack.handlers.log_tenant_quota_update",
        )

        signals.post_delete.connect(
            handlers.log_security_group_cleaned,
            sender=SecurityGroup,
            dispatch_uid="openstack.handlers.log_security_group_cleaned",
        )

        signals.post_delete.connect(
            handlers.log_security_group_rule_cleaned,
            sender=SecurityGroupRule,
            dispatch_uid="openstack.handlers.log_security_group_rule_cleaned",
        )

        signals.post_delete.connect(
            handlers.log_network_cleaned,
            sender=Network,
            dispatch_uid="openstack.handlers.log_network_cleaned",
        )

        signals.post_delete.connect(
            handlers.log_subnet_cleaned,
            sender=SubNet,
            dispatch_uid="openstack.handlers.log_subnet_cleaned",
        )

        signals.post_delete.connect(
            handlers.log_server_group_cleaned,
            sender=ServerGroup,
            dispatch_uid="openstack.handlers.log_server_group_cleaned",
        )

        Project.add_quota_field(
            name="os_cpu_count",
            quota_field=TotalQuotaField(
                target_models=[Instance],
                path_to_scope="project",
                target_field="cores",
            ),
        )

        Project.add_quota_field(
            name="os_ram_size",
            quota_field=TotalQuotaField(
                target_models=[Instance],
                path_to_scope="project",
                target_field="ram",
            ),
        )

        Project.add_quota_field(
            name="os_storage_size",
            quota_field=TotalQuotaField(
                target_models=[
                    Volume,
                    Snapshot,
                ],
                path_to_scope="project",
                target_field="size",
            ),
        )

        Customer.add_quota_field(
            name="os_cpu_count",
            quota_field=TotalQuotaField(
                target_models=[Instance],
                path_to_scope="project.customer",
                target_field="cores",
            ),
        )

        Customer.add_quota_field(
            name="os_ram_size",
            quota_field=TotalQuotaField(
                target_models=[Instance],
                path_to_scope="project.customer",
                target_field="ram",
            ),
        )

        Customer.add_quota_field(
            name="os_storage_size",
            quota_field=TotalQuotaField(
                target_models=[
                    Volume,
                    Snapshot,
                ],
                path_to_scope="project.customer",
                target_field="size",
            ),
        )

        for Resource in (
            Instance,
            Volume,
            Snapshot,
        ):
            name = Resource.__name__.lower()
            signals.post_save.connect(
                handlers.log_action,
                sender=Resource,
                dispatch_uid="openstack.handlers.log_%s_action" % name,
            )

        signals.post_save.connect(
            handlers.log_backup_schedule_creation,
            sender=BackupSchedule,
            dispatch_uid="openstack.handlers.log_backup_schedule_creation",
        )

        signals.post_save.connect(
            handlers.log_backup_schedule_action,
            sender=BackupSchedule,
            dispatch_uid="openstack.handlers.log_backup_schedule_action",
        )

        signals.pre_delete.connect(
            handlers.log_backup_schedule_deletion,
            sender=BackupSchedule,
            dispatch_uid="openstack.handlers.log_backup_schedule_deletion",
        )

        signals.post_save.connect(
            handlers.log_snapshot_schedule_creation,
            sender=SnapshotSchedule,
            dispatch_uid="openstack.handlers.log_snapshot_schedule_creation",
        )

        signals.post_save.connect(
            handlers.log_snapshot_schedule_action,
            sender=SnapshotSchedule,
            dispatch_uid="openstack.handlers.log_snapshot_schedule_action",
        )

        signals.pre_delete.connect(
            handlers.log_snapshot_schedule_deletion,
            sender=SnapshotSchedule,
            dispatch_uid="openstack.handlers.log_snapshot_schedule_deletion",
        )
