from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ServiceFixture
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import MoveResourceException, move_resource


class MoveResourceCommandTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = ServiceFixture()
        self.project = self.fixture.project
        self.resource = marketplace_factories.ResourceFactory(project=self.project)
        self.resource.scope = self.fixture.volume
        self.resource.save()
        resource_offering = self.resource.offering
        resource_offering.scope = self.fixture.service_settings
        resource_offering.save()
        self.order = marketplace_factories.OrderFactory(
            resource=self.resource, project=self.project
        )
        self.new_project = structure_factories.ProjectFactory()

        self.start_invoice = invoices_factories.InvoiceFactory(
            customer=self.project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.PENDING,
        )

        invoices_factories.InvoiceItemFactory(
            invoice=self.start_invoice,
            project=self.project,
            resource=self.resource,
        )

        self.target_invoice = invoices_factories.InvoiceFactory(
            customer=self.new_project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.PENDING,
        )

    def test_move_resource(self):
        self.assertEqual(self.start_invoice.items.count(), 1)
        self.assertEqual(self.target_invoice.items.count(), 0)
        move_resource(self.resource, self.new_project)

        self.fixture.volume.refresh_from_db()
        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.fixture.volume.project, self.new_project)
        self.assertEqual(self.order.project, self.new_project)
        self.assertEqual(self.resource.project, self.new_project)
        self.assertEqual(self.start_invoice.items.count(), 0)
        self.assertEqual(self.target_invoice.items.count(), 1)

    def test_resource_moving_is_not_possible_if_invoice_items_moving_is_not_possible(
        self,
    ):
        self.target_invoice.state = invoices_models.Invoice.States.CREATED
        self.target_invoice.save()
        self.assertRaises(
            MoveResourceException, move_resource, self.resource, self.new_project
        )

    def test_move_resource_if_target_invoice_does_not_exist(self):
        self.target_invoice.delete()
        move_resource(self.resource, self.new_project)

        target_invoice = invoices_models.Invoice.objects.get(
            customer=self.new_project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.PENDING,
        )

        self.fixture.volume.refresh_from_db()
        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.fixture.volume.project, self.new_project)
        self.assertEqual(self.order.project, self.new_project)
        self.assertEqual(self.resource.project, self.new_project)
        self.assertEqual(self.start_invoice.items.count(), 0)
        self.assertEqual(target_invoice.items.count(), 1)

    def test_move_request_based_resource(self):
        self.resource.scope = None
        self.resource.save()

        move_resource(self.resource, self.new_project)
        self.fixture.volume.refresh_from_db()
        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.order.project, self.new_project)
        self.assertEqual(self.resource.project, self.new_project)
        self.assertEqual(self.start_invoice.items.count(), 0)
        self.assertEqual(self.target_invoice.items.count(), 1)
