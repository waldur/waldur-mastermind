from django.test import TestCase

from nodeconductor_assembly_waldur.invoices import models

from .. import fixtures


class AddExpertRequestToInvoiceTest(TestCase):

    def setUp(self):
        self.fixture = fixtures.ExpertsFixture()
        self.contract = self.fixture.contract
        self.expert_request = self.contract.request

    def test_expert_request_item_is_created_if_request_is_completed(self):
        self.expert_request.state = self.expert_request.States.COMPLETED
        self.expert_request.save()

        self.assertTrue(models.GenericInvoiceItem.objects.filter(scope=self.expert_request))

    def test_expert_request_item_is_not_created_if_contract_is_missing(self):
        self.contract.delete()
        self.expert_request.state = self.expert_request.States.COMPLETED
        self.expert_request.contract = None
        self.expert_request.save()

        self.assertFalse(models.GenericInvoiceItem.objects.filter(scope=self.expert_request))

    def test_expert_request_item_is_not_created_if_request_is_not_completed(self):
        self.expert_request.state = self.expert_request.States.PENDING
        self.expert_request.save()

        self.assertFalse(models.GenericInvoiceItem.objects.filter(scope=self.expert_request))
