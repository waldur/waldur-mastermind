import datetime

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    ProjectRole,
)
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.utils import TestCreateProcessor


class OrderUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.admin
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            type="TEST_TYPE",
            shared=True,
            billable=True,
            customer=self.fixture.customer,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.order = factories.OrderFactory(
            project=self.project,
            created_by=self.fixture.manager,
            offering=self.offering,
            plan=self.plan,
            state=OrderStates.PENDING_CONSUMER,
            attributes={"old_key": "old_value"},
            limits={"cpu": 1},
        )
        # Register a test processor for limit validation
        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=TestCreateProcessor,
            can_update_limits=True,
        )
        models.OfferingComponent.objects.create(
            offering=self.offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
            limit_amount=10,
            limit_period=LimitPeriods.TOTAL,
        )

    def test_update_order_limits_and_attributes(self):
        url = factories.OrderFactory.get_url(self.order)
        new_attributes = {"new_key": "new_value"}
        new_limits = {"cpu": 5}
        new_start_date = (
            datetime.date.today() + datetime.timedelta(days=1)
        ).isoformat()
        payload = {
            "attributes": new_attributes,
            "limits": new_limits,
            "start_date": new_start_date,
        }

        # Grant approval permission
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.user)

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, new_attributes)
        self.assertEqual(self.order.limits["cpu"], 5)
        self.assertEqual(
            self.order.start_date, datetime.date.today() + datetime.timedelta(days=1)
        )

    def test_update_order_without_permission(self):
        # By default ADMIN does not have APPROVE_ORDER
        url = factories.OrderFactory.get_url(self.order)
        payload = {"limits": {"cpu": 5}}

        self.client.force_authenticate(self.user)
        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_order_in_wrong_state(self):
        self.order.state = OrderStates.EXECUTING
        self.order.save()

        url = factories.OrderFactory.get_url(self.order)
        payload = {"limits": {"cpu": 5}}

        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.user)

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_update_order_limits_validation(self):
        url = factories.OrderFactory.get_url(self.order)
        # 100 > 10 (limit)
        payload = {"limits": {"cpu": 100}}

        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.user)

        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
