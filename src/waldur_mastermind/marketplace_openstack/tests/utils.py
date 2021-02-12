import copy

from django.conf import settings
from django.test import override_settings
from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class BaseOpenStackTest(test.APITransactionTestCase):
    def setUp(self):
        super(BaseOpenStackTest, self).setUp()
        self.tenant_category = marketplace_factories.CategoryFactory(title='Tenant')
        self.instance_category = marketplace_factories.CategoryFactory(
            title='Instance', default_vm_category=True
        )
        self.volume_category = marketplace_factories.CategoryFactory(
            title='Volume', default_volume_category=True
        )

        self.decorator = override_plugin_settings(
            TENANT_CATEGORY_UUID=self.tenant_category.uuid.hex,
            INSTANCE_CATEGORY_UUID=self.instance_category.uuid.hex,
            VOLUME_CATEGORY_UUID=self.volume_category.uuid.hex,
        )
        self.decorator.enable()

    def tearDown(self):
        super(BaseOpenStackTest, self).tearDown()
        self.decorator.disable()


def override_plugin_settings(**kwargs):
    plugin_settings = copy.deepcopy(settings.WALDUR_MARKETPLACE_OPENSTACK)
    plugin_settings.update(kwargs)
    return override_settings(WALDUR_MARKETPLACE_OPENSTACK=plugin_settings)
