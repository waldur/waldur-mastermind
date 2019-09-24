from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_cost_planning.tests import factories as cost_planning_factories

from . import fixtures
from .. import models


class PackageDeploymentPlanTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.template = self.fixture.openstack_template

        self.plan = cost_planning_factories.DeploymentPlanFactory(project=self.fixture.project)
        self.url = cost_planning_factories.DeploymentPlanFactory.get_url(self.plan, action='evaluate')

        self.preset_param = {'ram': 10240, 'cores': 16, 'storage': 1024000}
        self.preset1 = cost_planning_factories.PresetFactory(**self.preset_param)

    def test_user_can_evaluate_deployment_plan(self):
        self._update_template_components(self.preset_param)
        self.plan.items.create(preset=self.preset1, quantity=1)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = response.data[0]

        settings_url = structure_factories.ServiceSettingsFactory.get_url(self.fixture.openstack_service_settings)
        self.assertEqual(plan['service_settings'], settings_url)

        template = plan['package_template']
        self.assertEqual(template['uuid'], self.template.uuid.hex)

        components = {component['type']: component['amount'] for component in template['components']}
        self.assertEqual(components['ram'], self.preset_param['ram'])
        self.assertEqual(components['cores'], self.preset_param['cores'])
        self.assertEqual(components['storage'], self.preset_param['storage'])

    def test_filter_package_template_if_items_quantity_more_one_and_template_component_meet_plan_requirements(self):
        quantity = 2

        for response in self._get_responses_for_different_template_components(masks=[(1, 1, 1), ],
                                                                              quantity=quantity):
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            plan = response.data[0]
            template = plan['package_template']
            self.assertEqual(template['uuid'], self.template.uuid.hex)

            components = {component['type']: component['amount'] for component in template['components']}
            self.assertEqual(components['ram'], self.preset_param['ram'] * quantity)
            self.assertEqual(components['cores'], self.preset_param['cores'] * quantity)
            self.assertEqual(components['storage'], self.preset_param['storage'] * quantity)

    def test_filter_package_template_if_items_quantity_more_one_and_template_component_dont_meet_plan_requirements(self):
        quantity = 2

        for response in self._get_responses_for_different_template_components(masks=[(0, 0, 0), (1, 0, 0), (1, 1, 0)],
                                                                              quantity=quantity):
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            plan = response.data[0]
            self.assertTrue('package_template' not in plan)

    def _get_responses_for_different_template_components(self, masks, quantity):
        """
        Return responses for different template components
        :param quantity: quantity items in plan
        :param masks: should be understood as follows:
            (0, 0, 0) - all parameters of component < plan requirements
            (1, 1, 1) - all parameters of component == plan requirements
            (1, 0, 0) - only first parameter of component == first plan requirement, others no
        """
        self.plan.items.create(preset=self.preset1, quantity=quantity)

        for k in masks:
            cmpt_param = {'ram': self.preset_param['ram'] * k[0] * quantity or self.preset_param['ram'],
                          'cores': self.preset_param['cores'] * k[1] * quantity or self.preset_param['cores'],
                          'storage': self.preset_param['storage'] * k[2] * quantity or self.preset_param['storage']
                          }
            self._update_template_components(cmpt_param)

            self.client.force_authenticate(self.fixture.staff)
            response = self.client.get(self.url)
            yield response

    def _update_template_components(self, source):
        type_component = [
            models.PackageComponent.Types.RAM,
            models.PackageComponent.Types.CORES,
            models.PackageComponent.Types.STORAGE
        ]
        for t in type_component:
            self.template.components.filter(type=t).update(amount=source[t])
