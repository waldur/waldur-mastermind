from waldur_core.logging.loggers import EventLogger, event_logger


class TenantQuotaLogger(EventLogger):
    quota_name = str
    tenant = 'openstack.Tenant'
    limit = int
    old_limit = int

    class Meta:
        event_types = ('openstack_tenant_quota_limit_updated',)
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        tenant = event_context['tenant']
        project = tenant.project
        return {tenant, project, project.customer}


class RouterLogger(EventLogger):
    router = 'openstack.Router'
    old_routes = list
    new_routes = list
    tenant_backend_id = str

    class Meta:
        event_types = ('openstack_router_updated',)
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        router = event_context['router']
        project = router.project
        return {project, project.customer}


class SecurityGroupLogger(EventLogger):
    security_group = 'openstack.SecurityGroup'

    class Meta:
        event_types = (
            'openstack_security_group_imported',
            'openstack_security_group_created',
            'openstack_security_group_updated',
            'openstack_security_group_pulled',
            'openstack_security_group_deleted',
            'openstack_security_group_cleaned',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        security_group = event_context['security_group']
        return {
            security_group,
            security_group.tenant,
        }


class SecurityGroupRuleLogger(EventLogger):
    security_group_rule = 'openstack.SecurityGroupRule'

    class Meta:
        event_types = (
            'openstack_security_group_rule_imported',
            'openstack_security_group_rule_created',
            'openstack_security_group_rule_updated',
            'openstack_security_group_rule_deleted',
            'openstack_security_group_rule_cleaned',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        security_group_rule = event_context['security_group_rule']
        return [
            security_group_rule,
            security_group_rule.security_group,
        ]


class ServerGroupLogger(EventLogger):
    server_group = 'openstack.ServerGroup'

    class Meta:
        event_types = (
            'openstack_server_group_imported',
            'openstack_server_group_pulled',
            'openstack_server_group_cleaned',
            'openstack_server_group_created',
            'openstack_server_group_deleted',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        server_group = event_context['server_group']
        return {
            server_group,
            server_group.tenant,
        }


class NetworkLogger(EventLogger):
    network = 'openstack.Network'

    class Meta:
        event_types = (
            'openstack_network_imported',
            'openstack_network_created',
            'openstack_network_updated',
            'openstack_network_pulled',
            'openstack_network_deleted',
            'openstack_network_cleaned',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        network = event_context['network']
        return {
            network,
            network.tenant,
        }


class SubNetLogger(EventLogger):
    subnet = 'openstack.SubNet'

    class Meta:
        event_types = (
            'openstack_subnet_created',
            'openstack_subnet_imported',
            'openstack_subnet_updated',
            'openstack_subnet_pulled',
            'openstack_subnet_deleted',
            'openstack_subnet_cleaned',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        subnet = event_context['subnet']
        return {
            subnet,
            subnet.network,
        }


class PortLogger(EventLogger):
    port = 'openstack.Port'

    class Meta:
        event_types = (
            'openstack_port_created',
            'openstack_port_imported',
            'openstack_port_pulled',
            'openstack_port_deleted',
            'openstack_port_cleaned',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        port = event_context['port']
        return {
            port,
            port.network,
        }


class FloatingIPLogger(EventLogger):
    floating_ip = 'openstack.FloatingIP'

    class Meta:
        event_types = (
            'openstack_floating_ip_attached',
            'openstack_floating_ip_detached',
            'openstack_floating_ip_description_updated',
        )
        event_groups = {
            'resources': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        floating_ip = event_context['floating_ip']
        port = event_context.get('port')
        return {floating_ip, floating_ip.tenant, port}


class FlavorLogger(EventLogger):
    flavor = 'openstack.Flavor'

    class Meta:
        event_types = (
            'openstack_flavor_created',
            'openstack_flavor_deleted',
        )
        event_groups = {
            'resources': event_types,
        }


event_logger.register('openstack_tenant_quota', TenantQuotaLogger)
event_logger.register('openstack_router', RouterLogger)
event_logger.register('openstack_network', NetworkLogger)
event_logger.register('openstack_subnet', SubNetLogger)
event_logger.register('openstack_security_group', SecurityGroupLogger)
event_logger.register('openstack_security_group_rule', SecurityGroupRuleLogger)
event_logger.register('openstack_server_group', ServerGroupLogger)
event_logger.register('openstack_port', PortLogger)
event_logger.register('openstack_floating_ip', FloatingIPLogger)
event_logger.register('openstack_flavor', FlavorLogger)
