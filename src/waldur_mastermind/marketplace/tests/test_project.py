import datetime

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import fixtures


class RemovalOfExpiredProjectWithoutActiveResourcesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource_1 = self.fixture.resource
        self.resource_1.state = models.Resource.States.OK
        self.resource_1.save()
        self.resource_2 = models.Resource.objects.create(
            project=self.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            state=models.Resource.States.OK,
        )
        self.project.end_date = datetime.datetime(year=2020, month=1, day=1).date()
        self.project.save()

    def test_delete_expired_project_if_every_resource_has_been_terminated(self):
        with freeze_time('2020-01-01'):
            self.assertTrue(self.project.is_expired)
            self.resource_1.state = models.Resource.States.TERMINATED
            self.resource_1.save()
            self.assertTrue(
                structure_models.Project.available_objects.filter(
                    id=self.project.id
                ).exists()
            )
            self.resource_2.state = models.Resource.States.TERMINATED
            self.resource_2.save()
            self.assertFalse(
                structure_models.Project.available_objects.filter(
                    id=self.project.id
                ).exists()
            )


class MarketplaceResourceCountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.resource.state = models.Resource.States.OK
        self.resource.save()

    def test_key_marketplace_resource_count_exists_in_project_response(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = structure_factories.ProjectFactory.get_url(self.fixture.resource.project)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        counters = response.json()['marketplace_resource_count']
        self.assertEqual(
            counters[self.resource.offering.category.uuid.hex],
            1,
        )
