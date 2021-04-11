import datetime

from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure import models as structure_models
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
                structure_models.Project.objects.filter(id=self.project.id).exists()
            )
            self.resource_2.state = models.Resource.States.TERMINATED
            self.resource_2.save()
            self.assertFalse(
                structure_models.Project.objects.filter(id=self.project.id).exists()
            )
