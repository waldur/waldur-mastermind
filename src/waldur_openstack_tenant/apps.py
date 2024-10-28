from django.apps import AppConfig


class OpenStackTenantConfig(AppConfig):
    name = "waldur_openstack_tenant"
    label = "openstack_tenant"
    verbose_name = "OpenStackTenant"
    service_name = "OpenStackTenant"
