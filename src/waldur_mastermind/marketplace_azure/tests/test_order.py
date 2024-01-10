from rest_framework import test

from waldur_azure import models as azure_models
from waldur_azure.tests import factories as azure_factories
from waldur_azure.tests import fixtures as azure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_azure import SQL_SERVER_TYPE, VIRTUAL_MACHINE_TYPE


class VirtualMachineCreateTest(test.APITransactionTestCase):
    def test_virtual_machine_is_created_when_order_is_processed(self):
        order = self.trigger_virtual_machine_creation()
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        self.assertTrue(azure_models.VirtualMachine.objects.exists())

    def test_request_payload_is_validated(self):
        order = self.trigger_virtual_machine_creation(
            name="Name should not contain spaces"
        )
        self.assertEqual(order.state, marketplace_models.Order.States.ERRED)

    def test_virtual_machine_state_is_synchronized(self):
        order = self.trigger_virtual_machine_creation()
        virtual_machine = order.resource.scope

        virtual_machine.begin_creating()
        virtual_machine.save()

        virtual_machine.set_ok()
        virtual_machine.save()

        order.refresh_from_db()
        self.assertEqual(order.state, order.States.DONE)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, marketplace_models.Resource.States.OK)

        order.refresh_from_db()
        self.assertEqual(order.state, marketplace_models.Order.States.DONE)

    def trigger_virtual_machine_creation(self, **kwargs):
        fixture = azure_fixtures.AzureFixture()
        service_settings = fixture.settings

        azure_models.SizeAvailabilityZone.objects.create(
            size=fixture.size, location=fixture.location, zone=1
        )

        attributes = {
            "size": azure_factories.SizeFactory.get_url(fixture.size),
            "image": azure_factories.ImageFactory.get_url(fixture.image),
            "name": "virtual-machine",
            "location": azure_factories.LocationFactory.get_url(fixture.location),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=VIRTUAL_MACHINE_TYPE, scope=service_settings
        )
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )

        marketplace_utils.process_order(order, fixture.staff)

        order.refresh_from_db()
        return order


class SQLServerCreateTest(test.APITransactionTestCase):
    def test_sql_server_is_created_when_order_is_processed(self):
        order = self.trigger_resource_creation()
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        self.assertTrue(azure_models.SQLServer.objects.exists())

    def test_request_payload_is_validated(self):
        order = self.trigger_resource_creation(name="Name should not contain spaces")
        self.assertEqual(order.state, marketplace_models.Order.States.ERRED)

    def test_sql_server_state_is_synchronized(self):
        order = self.trigger_resource_creation()
        sql_server = order.resource.scope

        sql_server.begin_creating()
        sql_server.save()

        sql_server.set_ok()
        sql_server.save()

        order.refresh_from_db()
        self.assertEqual(order.state, order.States.DONE)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, marketplace_models.Resource.States.OK)

        order.refresh_from_db()
        self.assertEqual(order.state, marketplace_models.Order.States.DONE)

    def trigger_resource_creation(self, **kwargs):
        fixture = azure_fixtures.AzureFixture()
        service_settings = fixture.settings

        attributes = {
            "name": "database-server",
            "location": azure_factories.LocationFactory.get_url(),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=SQL_SERVER_TYPE, scope=service_settings
        )
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )

        marketplace_utils.process_order(order, fixture.staff)

        order.refresh_from_db()
        return order
