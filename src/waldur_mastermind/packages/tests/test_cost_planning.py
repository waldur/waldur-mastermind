from rest_framework import status, test

from nodeconductor.structure.tests import factories as structure_factories
from waldur_cost_planning.tests import factories as cost_planning_factories

from . import fixtures, factories
from .. import models


class PackageDeploymentPlanTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.PackageFixture()
        self.template = self.fixture.openstack_template
        self.template.components.filter(type=models.PackageComponent.Types.RAM).update(amount=20480)
        self.template.components.filter(type=models.PackageComponent.Types.CORES).update(amount=20)
        self.template.components.filter(type=models.PackageComponent.Types.STORAGE).update(amount=1024000)

        self.plan = cost_planning_factories.DeploymentPlanFactory(project=self.fixture.project)
        self.url = cost_planning_factories.DeploymentPlanFactory.get_url(self.plan, action='evaluate')

        self.preset1 = cost_planning_factories.PresetFactory(ram=10240, cores=16, storage=1024000)
        self.plan.items.create(preset=self.preset1, quantity=1)

    def test_user_can_evaluate_deployment_plan(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan = response.data[0]

        settings_url = structure_factories.ServiceSettingsFactory.get_url(self.fixture.openstack_service_settings)
        self.assertEqual(plan['service_settings'], settings_url)

        template = plan['package_template']
        self.assertEqual(template['url'], factories.PackageTemplateFactory.get_url(self.template))

        components = {component['type']: component['amount'] for component in template['components']}
        self.assertEqual(components['ram'], 20480)
        self.assertEqual(components['cores'], 20)
        self.assertEqual(components['storage'], 1024000)
