from rest_framework import test

from waldur_core.quotas import signals as quota_signals
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class AggregateResourceCountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.plan = factories.PlanFactory()
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.plan.offering,
            plan=self.plan,
        )
        self.category = self.plan.offering.category

    def test_when_resource_scope_is_updated_resource_count_is_increased(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.project, category=self.category
            ).count,
            1,
        )
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.customer, category=self.category
            ).count,
            1,
        )

    def test_when_resource_scope_is_updated_resource_count_is_decreased(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        self.resource.state = models.Resource.States.TERMINATED
        self.resource.save()

        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.project, category=self.category
            ).count,
            0,
        )
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.customer, category=self.category
            ).count,
            0,
        )

    def test_recalculate_count(self):
        self.resource.scope = self.fixture.resource
        self.resource.save()
        models.AggregateResourceCount.objects.all().delete()
        quota_signals.recalculate_quotas.send(sender=self)

        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.project, category=self.category
            ).count,
            1,
        )
        self.assertEqual(
            models.AggregateResourceCount.objects.get(
                scope=self.customer, category=self.category
            ).count,
            1,
        )
