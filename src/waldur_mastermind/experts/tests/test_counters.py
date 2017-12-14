from rest_framework import test, status

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories
from .. import models


class BaseCountersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.expert_request = factories.ExpertRequestFactory(project=self.fixture.project)

    def assert_has_experts(self, count):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.get_url(), {'fields': ['experts']})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'experts': count})

    def get_url(self):
        raise NotImplementedError


class ProjectCountersTest(BaseCountersTest):
    def get_url(self):
        return structure_factories.ProjectFactory.get_url(self.fixture.project, action='counters')

    def test_pending_request(self):
        self.assert_has_experts(1)

    def test_active_request(self):
        self.expert_request.state = models.ExpertRequest.States.ACTIVE
        self.expert_request.save()
        self.assert_has_experts(1)

    def test_cancelled_request(self):
        self.expert_request.state = models.ExpertRequest.States.CANCELLED
        self.expert_request.save()
        self.assert_has_experts(0)


class CustomerCountersTest(BaseCountersTest):
    def get_url(self):
        return structure_factories.CustomerFactory.get_url(self.fixture.customer, action='counters')

    def test_pending_request(self):
        self.assert_has_experts(1)

    def test_active_request(self):
        expert_request = factories.ExpertRequestFactory(state=models.ExpertRequest.States.ACTIVE)
        models.ExpertContract.objects.create(request=expert_request, team=self.fixture.project)
        self.assert_has_experts(2)
