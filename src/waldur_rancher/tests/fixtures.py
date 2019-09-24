from django.utils.functional import cached_property
from django.contrib.contenttypes.models import ContentType

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .. import models


class RancherFixture(ProjectFixture):
    def __init__(self):
        super(RancherFixture, self).__init__()
        self.node

    @cached_property
    def settings(self):
        return factories.RancherServiceSettingsFactory(customer=self.customer)

    @cached_property
    def service(self):
        return factories.RancherServiceFactory(customer=self.customer, settings=self.settings)

    @cached_property
    def spl(self):
        return factories.RancherServiceProjectLinkFactory(service=self.service, project=self.project)

    @cached_property
    def cluster(self):
        return factories.ClusterFactory(service_project_link=self.spl, state=models.Cluster.States.OK)

    @cached_property
    def instance(self):
        settings = structure_factories.ServiceSettingsFactory(customer=self.customer)
        service = structure_factories.TestServiceFactory(customer=self.customer, settings=settings)
        spl = structure_factories.TestServiceProjectLinkFactory(service=service, project=self.project)
        return structure_factories.TestNewInstanceFactory(service_project_link=spl)

    @cached_property
    def node(self):
        content_type = ContentType.objects.get_for_model(self.instance)
        return models.Node.objects.create(
            cluster=self.cluster,
            object_id=self.instance.id,
            content_type=content_type
        )
