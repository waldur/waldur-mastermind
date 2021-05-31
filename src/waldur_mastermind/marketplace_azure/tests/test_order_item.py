from django.core.exceptions import ObjectDoesNotExist
from rest_framework import test

from waldur_azure import models as azure_models
from waldur_azure.tests import factories as azure_factories
from waldur_azure.tests import fixtures as azure_fixtures
from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_azure import SQL_SERVER_TYPE, VIRTUAL_MACHINE_TYPE


class VirtualMachineCreateTest(test.APITransactionTestCase):
    def test_virtual_machine_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_virtual_machine_creation()
        self.assertEqual(
            order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertTrue(azure_models.VirtualMachine.objects.exists())

    def test_request_payload_is_validated(self):
        order_item = self.trigger_virtual_machine_creation(
            name='Name should not contain spaces'
        )
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)

    def test_virtual_machine_state_is_synchronized(self):
        order_item = self.trigger_virtual_machine_creation()
        virtual_machine = order_item.resource.scope

        virtual_machine.begin_creating()
        virtual_machine.save()

        virtual_machine.set_ok()
        virtual_machine.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

        order_item.resource.refresh_from_db()
        self.assertEqual(
            order_item.resource.state, marketplace_models.Resource.States.OK
        )

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

    def trigger_virtual_machine_creation(self, **kwargs):
        fixture = azure_fixtures.AzureFixture()
        service_settings = fixture.settings

        attributes = {
            'size': azure_factories.SizeFactory.get_url(fixture.size),
            'image': azure_factories.ImageFactory.get_url(fixture.image),
            'name': 'virtual-machine',
            'location': azure_factories.LocationFactory.get_url(fixture.location),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=VIRTUAL_MACHINE_TYPE, scope=service_settings
        )
        order = marketplace_factories.OrderFactory(
            project=fixture.project, state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            offering=offering, attributes=attributes, order=order,
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        return order_item


class VirtualMachineDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = azure_fixtures.AzureFixture()
        self.virtual_machine = self.fixture.virtual_machine
        self.offering = marketplace_factories.OfferingFactory(type=VIRTUAL_MACHINE_TYPE)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.virtual_machine, offering=self.offering
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.virtual_machine.state,
            azure_models.VirtualMachine.States.DELETION_SCHEDULED,
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.virtual_machine.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.virtual_machine.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.virtual_machine.refresh_from_db()


class SQLServerCreateTest(test.APITransactionTestCase):
    def test_sql_server_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_resource_creation()
        self.assertEqual(
            order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertTrue(azure_models.SQLServer.objects.exists())

    def test_request_payload_is_validated(self):
        order_item = self.trigger_resource_creation(
            name='Name should not contain spaces'
        )
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)

    def test_virtual_machine_state_is_synchronized(self):
        order_item = self.trigger_resource_creation()
        sql_server = order_item.resource.scope

        sql_server.begin_creating()
        sql_server.save()

        sql_server.set_ok()
        sql_server.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

        order_item.resource.refresh_from_db()
        self.assertEqual(
            order_item.resource.state, marketplace_models.Resource.States.OK
        )

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

    def trigger_resource_creation(self, **kwargs):
        fixture = azure_fixtures.AzureFixture()
        service_settings = fixture.settings

        attributes = {
            'name': 'database-server',
            'location': azure_factories.LocationFactory.get_url(),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=SQL_SERVER_TYPE, scope=service_settings
        )
        order = marketplace_factories.OrderFactory(
            project=fixture.project, state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            offering=offering, attributes=attributes, order=order,
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        return order_item


class SQLServerDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = azure_fixtures.AzureFixture()
        self.sql_server = self.fixture.sql_server
        self.offering = marketplace_factories.OfferingFactory(type=SQL_SERVER_TYPE)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.sql_server, offering=self.offering
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.sql_server.state, azure_models.VirtualMachine.States.DELETION_SCHEDULED
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.sql_server.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.sql_server.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.sql_server.refresh_from_db()
