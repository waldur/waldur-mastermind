import copy
from unittest import mock

from django.conf import settings
from django.test import override_settings
from rest_framework import test

from waldur_core.core import tasks as core_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import executors


class BaseOpenStackTest(test.APITransactionTestCase):
    def setUp(self):
        super(BaseOpenStackTest, self).setUp()
        self.tenant_category = marketplace_factories.CategoryFactory(title='Tenant')
        self.instance_category = marketplace_factories.CategoryFactory(title='Instance')
        self.volume_category = marketplace_factories.CategoryFactory(title='Volume')

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


def run_openstack_package_change_executor(package, new_template):
    with mock.patch.object(core_tasks.BackendMethodTask, 'get_backend'):
        executors.OpenStackPackageChangeExecutor.execute(
            package.tenant,
            new_template=new_template,
            old_package=package,
            service_settings=package.service_settings,
            is_async=False,
        )
