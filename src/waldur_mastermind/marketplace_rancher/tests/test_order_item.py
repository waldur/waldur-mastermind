from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import fixtures, factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME
from waldur_rancher.tests import factories as rancher_factories
from waldur_rancher import models as rancher_models


class OrderItemProcessedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_resource_is_created_when_order_item_is_processed(self):
        service = rancher_factories.RancherServiceFactory(customer=self.fixture.customer)
        spl = rancher_factories.RancherServiceProjectLinkFactory(project=self.fixture.project, service=service)
        service_settings = spl.service.settings
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME, scope=service_settings)
        instance = self._create_new_test_instance()
        order = marketplace_factories.OrderFactory(project=self.fixture.project, created_by=self.fixture.owner)
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            attributes={'name': 'name',
                        'instance': structure_factories.TestNewInstanceFactory.get_url(instance),
                        }
        )
        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)
        self.assertTrue(marketplace_models.Resource.objects.filter(name='name').exists())
        self.assertTrue(rancher_models.Cluster.objects.filter(name='name').exists())

    def _create_new_test_instance(self):
        customer = self.fixture.customer
        settings = structure_factories.ServiceSettingsFactory(customer=customer)
        service = structure_factories.TestServiceFactory(customer=customer, settings=settings)
        spl = structure_factories.TestServiceProjectLinkFactory(service=service, project=self.fixture.project)
        return structure_factories.TestNewInstanceFactory(service_project_link=spl)
