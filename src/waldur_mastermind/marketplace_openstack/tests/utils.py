import copy

from django.conf import settings
from django.test import override_settings
from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class BaseOpenStackTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tenant_category = marketplace_factories.CategoryFactory(
            title='Tenant', default_tenant_category=True
        )
        self.instance_category = marketplace_factories.CategoryFactory(
            title='Instance', default_vm_category=True
        )
        self.volume_category = marketplace_factories.CategoryFactory(
            title='Volume', default_volume_category=True
        )


def override_plugin_settings(**kwargs):
    plugin_settings = copy.deepcopy(settings.WALDUR_MARKETPLACE_OPENSTACK)
    plugin_settings.update(kwargs)
    return override_settings(WALDUR_MARKETPLACE_OPENSTACK=plugin_settings)
