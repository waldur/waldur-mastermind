from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.fixtures import ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.utils import (
    get_current_month,
    get_current_year,
)


@ddt
@freeze_time("2024-03-06")
class GrowthTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.UserFixture()
        self.url = reverse("invoice-list") + "growth/"
        self.customer1 = structure_factories.CustomerFactory()
        self.customer2 = structure_factories.CustomerFactory()
        models.Invoice.objects.create(
            customer=self.customer1,
            year=get_current_year(),
            month=get_current_month() - 2,
            total_cost=10,
            total_price=10,
        )
        models.Invoice.objects.create(
            customer=self.customer2,
            year=get_current_year(),
            month=get_current_month() - 1,
            total_cost=20,
            total_price=20,
        )
        models.Invoice.objects.create(
            customer=self.customer1,
            year=get_current_year(),
            month=get_current_month(),
            total_cost=10,
            total_price=10,
        )
        models.Invoice.objects.create(
            customer=self.customer2,
            year=get_current_year(),
            month=get_current_month(),
            total_cost=20,
            total_price=20,
        )

        self.user1 = structure_factories.UserFactory()
        self.customer1.add_user(self.user1, ServiceProviderRole.MANAGER)
        self.user2 = structure_factories.UserFactory()

    def test_user_can_see_growth_stats_of_connected_customer(self):
        self.client.force_authenticate(self.user1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify total periods for Customer1 (user1 connected to customer1):
        self.assertEqual(response.data["total_periods"][-1], 10)  # Current month
        self.assertEqual(response.data["total_periods"][-2], 0)  # One month ago
        self.assertEqual(response.data["total_periods"][-3], 10)  # Two months ago

    def test_user_cannot_see_growth_stats_of_other_customers(self):
        self.client.force_authenticate(self.user2)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # user is not connected to any customer, so user cannot get actual data
        self.assertEqual(response.data["total_periods"][-1], 0)
        self.assertEqual(response.data["total_periods"][-2], 0)
        self.assertEqual(response.data["total_periods"][-3], 0)

    @data("staff", "global_support")
    def test_staff_support_user_can_see_growth_stats_of_all_customers(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify total periods for all customers:
        self.assertEqual(response.data["total_periods"][-1], 30)  # Current month
        self.assertEqual(response.data["total_periods"][-2], 20)  # One month ago
        self.assertEqual(response.data["total_periods"][-3], 10)  # Two months ago
