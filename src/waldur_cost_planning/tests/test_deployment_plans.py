from ddt import ddt, data
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories

from . import factories, fixtures
from .. import models


@ddt
class DeploymentPlanListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CostPlanningFixture()
        self.deployment_plan = self.fixture.deployment_plan

    @data('staff', 'owner', 'global_support', 'manager', 'admin')
    def test_user_with_permissions_can_list_deployment_plans(self, user):
        response = self.get_deployment_plans(getattr(self.fixture, user))
        self.assertEqual(len(response.data), 1)

    @data('user')
    def test_user_without_permissions_cannot_list_deployment_plans(self, user):
        response = self.get_deployment_plans(getattr(self.fixture, user))
        self.assertEqual(len(response.data), 0)

    def test_deployment_plans_can_be_filtered_by_customer(self):
        self.client.force_authenticate(self.fixture.staff)
        customer = structure_factories.CustomerFactory()
        response = self.client.get(factories.DeploymentPlanFactory.get_list_url(), {
            'customer_uuid': customer.uuid.hex
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def get_deployment_plans(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(factories.DeploymentPlanFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response


@ddt
class DeploymentPlanCreateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.CostPlanningFixture()

    @data('owner', 'staff', 'admin', 'manager')
    def test_user_with_permissions_can_create_deployment_plan(self, user):
        response = self.create_deployment_plan(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        plan = models.DeploymentPlan.objects.get(uuid=response.data['uuid'])
        self.assertEqual(1, plan.items.count())

    @data('global_support')
    def test_user_without_permissions_cannot_create_deployment_plan(self, user):
        response = self.create_deployment_plan(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def create_deployment_plan(self, user):
        self.client.force_authenticate(user=user)
        return self.client.post(factories.DeploymentPlanFactory.get_list_url(), {
            'project': structure_factories.ProjectFactory.get_url(self.fixture.project),
            'name': 'Webapp for Monster Inc.',
            'items': [
                {
                    'preset': factories.PresetFactory.get_url(),
                    'quantity': 1
                }
            ]
        })


@ddt
class DeploymentPlanUpdateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.CostPlanningFixture()
        self.plan = self.fixture.deployment_plan

        self.preset1 = factories.PresetFactory()
        self.plan.items.create(preset=self.preset1, quantity=1)
        self.preset2 = factories.PresetFactory()
        self.plan.items.create(preset=self.preset2, quantity=2)

        self.url = factories.DeploymentPlanFactory.get_url(self.plan)

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_with_permissions_can_update_item_list(self, user):
        """
        Old item is removed, remaining item is updated.
        """
        self.client.force_authenticate(user=getattr(self.fixture, user))
        item = {
            'preset': factories.PresetFactory.get_url(self.preset1),
            'quantity': 2
        }

        response = self.client.patch(self.url, {'items': [item]})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.items.count(), 1)
        self.assertEqual(self.plan.items.first().quantity, item['quantity'])

    @data('global_support')
    def test_user_without_permissions_cannot_update_plan(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.patch(self.url, {'name': 'New name for plan'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner')
    def test_user_with_permissions_can_update_name(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.patch(self.url, {
            'name': 'New name for plan'
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.name, 'New name for plan')


@ddt
class DeploymentPlanDeleteTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.CostPlanningFixture()
        self.plan = self.fixture.deployment_plan
        self.url = factories.DeploymentPlanFactory.get_url(self.plan)

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_with_permissions_can_delete_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.DeploymentPlan.objects.filter(pk=self.plan.pk).exists())

    @data('global_support')
    def test_user_without_permissions_cannot_delete_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.DeploymentPlan.objects.filter(pk=self.plan.pk).exists())


@ddt
class DeploymentPlanEvaluateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.CostPlanningFixture()
        self.plan = self.fixture.deployment_plan
        self.url = factories.DeploymentPlanFactory.get_url(self.plan, action='evaluate')

    @data('staff', 'global_support', 'owner', 'admin', 'manager')
    def test_user_with_permission_can_evaluate_deployment_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_without_permission_cannot_eveluate_deployment_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
