from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_vmware import signals
from waldur_vmware.tests.fixtures import VMwareFixture


class HandlersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = VMwareFixture()
        self.vm = self.fixture.virtual_machine
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.vm, project=self.fixture.project
        )

    def test_when_vm_is_updated_marketplace_resource_limits_are_updated(self):
        # Arrange
        self.vm.cores = 10
        self.vm.ram = 10240

        # Act
        signals.vm_updated.send(self.__class__, vm=self.vm)

        # Assert
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["cpu"], self.vm.cores)
        self.assertEqual(self.resource.limits["ram"], self.vm.ram)
