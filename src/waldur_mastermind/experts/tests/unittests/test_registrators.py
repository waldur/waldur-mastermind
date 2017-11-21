from django.test import TestCase
from django.utils import timezone

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.experts import registrators, models

from .. import fixtures


class ExpertRequestRegistratorRegisterTest(TestCase):

    def setUp(self):
        self.fixture = fixtures.ExpertsFixture()
        self.registrator = registrators.ExpertRequestRegistrator()
        self.invoice = invoice_factories.InvoiceFactory()

    def test_item_is_not_registered_if_it_is_project_based_and_was_registered_before(self):
        expert_request = self.fixture.contract.request
        expert_request.state = models.ExpertRequest.States.COMPLETED
        expert_request.recurring_billing = False
        expert_request.save()

        self.assertEqual(invoice_models.GenericInvoiceItem.objects.filter(scope=expert_request).count(), 1)

        self.registrator.register([expert_request], self.invoice, timezone.now())

        self.assertEqual(invoice_models.GenericInvoiceItem.objects.filter(scope=expert_request).count(), 1)
