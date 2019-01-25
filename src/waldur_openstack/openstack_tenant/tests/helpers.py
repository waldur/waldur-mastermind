import copy

from django.conf import settings
from django.test import override_settings


def override_openstack_tenant_settings(**kwargs):
    os_settings = copy.deepcopy(settings.WALDUR_OPENSTACK_TENANT)
    os_settings.update(kwargs)
    return override_settings(WALDUR_OPENSTACK_TENANT=os_settings)
