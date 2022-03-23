from django.apps import AppConfig
from django.db.models import signals


class OpenStackConfig(AppConfig):
    """OpenStack is a toolkit for building private and public clouds.
    This application adds support for managing OpenStack deployments -
    tenants, instances, security groups and networks.
    """

    name = 'waldur_openstack.openstack'
    label = 'openstack'
    verbose_name = 'OpenStack'
    service_name = 'OpenStack'

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.quotas.fields import QuotaField
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.registry import SupportedServices
        from . import handlers

        Tenant = self.get_model('Tenant')
        Network = self.get_model('Network')
        SubNet = self.get_model('SubNet')
        SecurityGroup = self.get_model('SecurityGroup')
        SecurityGroupRule = self.get_model('SecurityGroupRule')
        ServerGroup = self.get_model('ServerGroup')

        # structure
        from .backend import OpenStackBackend

        SupportedServices.register_backend(OpenStackBackend)

        signals.post_save.connect(
            handlers.clear_cache_when_service_settings_are_updated,
            sender=structure_models.ServiceSettings,
            dispatch_uid='openstack.handlers.clear_cache_when_service_settings_are_updated',
        )

        from . import quotas

        quotas.inject_tenant_quotas()

        for resource in ('vcpu', 'ram', 'storage'):
            structure_models.ServiceSettings.add_quota_field(
                name='openstack_%s' % resource,
                quota_field=QuotaField(
                    creation_condition=lambda service_settings: service_settings.type
                    == OpenStackConfig.service_name
                ),
            )

        for model in (structure_models.Project, structure_models.Customer):
            structure_signals.structure_role_revoked.connect(
                handlers.remove_ssh_key_from_tenants,
                sender=model,
                dispatch_uid='openstack.handlers.remove_ssh_key_from_tenants__%s'
                % model.__name__,
            )

        signals.pre_delete.connect(
            handlers.remove_ssh_key_from_all_tenants_on_it_deletion,
            sender=core_models.SshPublicKey,
            dispatch_uid='openstack.handlers.remove_ssh_key_from_all_tenants_on_it_deletion',
        )

        from waldur_core.quotas.models import Quota

        signals.post_save.connect(
            handlers.log_tenant_quota_update,
            sender=Quota,
            dispatch_uid='openstack.handlers.log_tenant_quota_update',
        )

        signals.post_save.connect(
            handlers.update_service_settings_name,
            sender=Tenant,
            dispatch_uid='openstack.handlers.update_service_settings_name',
        )

        signals.post_delete.connect(
            handlers.log_security_group_cleaned,
            sender=SecurityGroup,
            dispatch_uid='openstack.handlers.log_security_group_cleaned',
        )

        signals.post_delete.connect(
            handlers.log_security_group_rule_cleaned,
            sender=SecurityGroupRule,
            dispatch_uid='openstack.handlers.log_security_group_rule_cleaned',
        )

        signals.post_delete.connect(
            handlers.log_network_cleaned,
            sender=Network,
            dispatch_uid='openstack.handlers.log_network_cleaned',
        )

        signals.post_delete.connect(
            handlers.log_subnet_cleaned,
            sender=SubNet,
            dispatch_uid='openstack.handlers.log_subnet_cleaned',
        )

        signals.post_delete.connect(
            handlers.log_server_group_cleaned,
            sender=ServerGroup,
            dispatch_uid='openstack.handlers.log_server_group_cleaned',
        )
