from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_openstack.openstack_tenant.tests.fixtures import OpenStackTenantFixture

from .. import tasks
from .utils import BaseOpenStackTest


class TaskTest(BaseOpenStackTest):
    def setUp(self):
        super(TaskTest, self).setUp()
        self.fixture = OpenStackTenantFixture()
        self.offering = marketplace_factories.OfferingFactory()
        self.offering.scope = self.fixture.instance.service_settings
        self.offering.type = INSTANCE_TYPE
        self.offering.save()

    def test_create_resources_for_lost_instances_and_volumes(self):
        tasks.create_resources_for_lost_instances_and_volumes()
        self.assertTrue(
            marketplace_models.Resource.objects.filter(offering=self.offering).exists()
        )
