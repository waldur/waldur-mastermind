import mock

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import models
from . import factories


class ExpertContractProjectCacheTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.expert_request = factories.ExpertRequestFactory()
        self.expert_team = structure_factories.ProjectFactory()
        self.expert_contract = models.ExpertContract.objects.create(
            request=self.expert_request,
            team=self.expert_team,
        )

    def test_cache_is_populated_when_contract_is_created(self):
        self.assertEqual(self.expert_contract.team_name, self.expert_contract.team.name)
        self.assertEqual(self.expert_contract.team_uuid, self.expert_contract.team.uuid.hex)
        self.assertEqual(self.expert_contract.team_customer, self.expert_contract.team.customer)

    def test_cache_is_updated_when_project_is_renamed(self):
        self.expert_contract.team.name = 'NEW PROJECT NAME'
        self.expert_contract.team.save(update_fields=['name'])

        self.expert_contract.refresh_from_db()
        self.assertEqual(self.expert_contract.team_name, self.expert_contract.team.name)

    def test_contract_is_not_removed_when_project_is_removed(self):
        self.expert_contract.team.delete()
        self.assertTrue(models.ExpertContract.objects.filter(id=self.expert_contract.id))

    def test_contract_is_removed_when_customer_is_removed(self):
        self.expert_contract.team.delete()
        self.expert_contract.team_customer.delete()
        self.assertFalse(models.ExpertContract.objects.filter(id=self.expert_contract.id))


class CreatePDFContractTest(test.APITransactionTestCase):
    @mock.patch('waldur_mastermind.experts.tasks.create_pdf_contract')
    def test_task_create_pdf_contract_must_be_call_if_bid_was_accepted(self, create_pdf_contract_mock):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.expert_bid = factories.ExpertBidFactory()
        self.client.force_authenticate(self.staff)
        self.accept_bid()
        self.assertEqual(create_pdf_contract_mock.delay.call_count, 1)

    def accept_bid(self):
        url = factories.ExpertBidFactory.get_url(self.expert_bid, 'accept')
        return self.client.post(url)
