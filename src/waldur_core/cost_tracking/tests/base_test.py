from rest_framework import test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .. import CostTrackingRegister, models


class BaseCostTrackingTest(test.APITransactionTestCase):

    def setUp(self):
        self.users = {
            'staff': structure_factories.UserFactory(username='staff', is_staff=True),
            'owner': structure_factories.UserFactory(username='owner'),
            'administrator': structure_factories.UserFactory(username='administrator'),
            'manager': structure_factories.UserFactory(username='manager'),
        }

        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.users['owner'], structure_models.CustomerRole.OWNER)
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.project.add_user(self.users['administrator'], structure_models.ProjectRole.ADMINISTRATOR)
        self.project.add_user(self.users['manager'], structure_models.ProjectRole.MANAGER)

        self.service = structure_factories.TestServiceFactory(customer=self.customer)
        self.service_project_link = structure_factories.TestServiceProjectLinkFactory(
            project=self.project, service=self.service)

        CostTrackingRegister.register_strategy(factories.TestNewInstanceCostTrackingStrategy)
        models.DefaultPriceListItem.init_from_registered_resources()
