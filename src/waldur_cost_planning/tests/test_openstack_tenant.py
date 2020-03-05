from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.cost_tracking import models as ct_models
from waldur_core.cost_tracking.tests import factories as ct_factories
from waldur_openstack.openstack_tenant import cost_tracking as ot_cost_tracking
from waldur_openstack.openstack_tenant import models as ot_models
from waldur_openstack.openstack_tenant.tests import factories as ot_factories

from . import factories, fixtures


@ddt
class OpenStackTenantOptimizerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CostPlanningOpenStackPluginFixture()
        self.plan = self.fixture.deployment_plan
        self.url = factories.DeploymentPlanFactory.get_url(self.plan, action='evaluate')
        self.service = self.fixture.spl.service
        self.settings = self.service.settings
        self.flavor_params = [
            {'cores': 1, 'ram': 1 * 1024, 'name': 'flavor-1'},
            {'cores': 2, 'ram': 2 * 1024, 'name': 'flavor-2'},
            {'cores': 2, 'ram': 3 * 1024, 'name': 'flavor-3'},
            {'cores': 4, 'ram': 4 * 1024, 'name': 'flavor-4'},
        ]

        for p in self.flavor_params:
            ot_factories.FlavorFactory(settings=self.settings, **p)
            ct_models.DefaultPriceListItem.objects.update_or_create(
                resource_content_type=ContentType.objects.get_for_model(
                    ot_models.Instance
                ),
                item_type='flavor',
                key=p['name'],
            )

        ct_factories.DefaultPriceListItemFactory(
            resource_content_type=ContentType.objects.get_for_model(ot_models.Volume),
            item_type=ot_cost_tracking.VolumeStrategy.Types.STORAGE,
            key=ot_cost_tracking.VolumeStrategy.Keys.STORAGE,
        )

    @data(
        {'variant': 'small', 'cores': 2, 'ram': 1 * 1024},
        {'variant': 'medium', 'cores': 2, 'ram': 4 * 1024},
    )
    def test_positive_case(self, preset_param):
        response = self._get_response(preset_param)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data[0]['error_message'])

    @data({'variant': 'large', 'cores': 8, 'ram': 4 * 1024},)
    def test_negative_case(self, preset_param):
        response = self._get_response(preset_param)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data[0]['error_message'])
        self.assertTrue('It is too big' in response.data[0]['error_message'])

    def _get_response(self, preset_param):
        self.preset = factories.PresetFactory(
            category=self.fixture.category, **preset_param
        )
        factories.DeploymentPlanItemFactory(plan=self.plan, preset=self.preset)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        return response
