from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openstack.openstack_tenant.tests import factories as ot_factories

from . import factories


class CostPlanningFixture(structure_fixtures.ProjectFixture):

    @cached_property
    def category(self):
        return factories.CategoryFactory()

    @cached_property
    def preset(self):
        return factories.PresetFactory(category=self.category)

    @cached_property
    def deployment_plan(self):
        return factories.DeploymentPlanFactory(project=self.project)


class CostPlanningOpenStackPluginFixture(CostPlanningFixture):

    @cached_property
    def spl(self):
        return ot_factories.OpenStackTenantServiceProjectLinkFactory(project=self.project)
