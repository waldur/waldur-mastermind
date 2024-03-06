from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.utils import (
    get_current_month,
    get_current_year,
)


@freeze_time("2024-03-06")
class GrowthTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("invoice-list") + "growth/"
        self.user = structure_factories.UserFactory()

    def test_positive(self):
        customer1 = structure_factories.CustomerFactory()
        customer2 = structure_factories.CustomerFactory()
        models.Invoice.objects.create(
            customer=customer1,
            year=get_current_year(),
            month=get_current_month() - 2,
            total_cost=10,
            total_price=10,
        )
        models.Invoice.objects.create(
            customer=customer2,
            year=get_current_year(),
            month=get_current_month() - 1,
            total_cost=20,
            total_price=20,
        )
        models.Invoice.objects.create(
            customer=customer1,
            year=get_current_year(),
            month=get_current_month(),
            total_cost=10,
            total_price=10,
        )
        models.Invoice.objects.create(
            customer=customer2,
            year=get_current_year(),
            month=get_current_month(),
            total_cost=20,
            total_price=20,
        )
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_periods"][-1], 30)
        self.assertEqual(response.data["total_periods"][-2], 20)
        self.assertEqual(response.data["total_periods"][-3], 10)
