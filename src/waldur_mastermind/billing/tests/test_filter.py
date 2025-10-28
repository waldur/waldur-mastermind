import datetime
import unittest

from django.utils import timezone
from rest_framework import test
from rest_framework.settings import api_settings

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.billing import models
from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class CustomerEstimatedCostFilterTest(test.APITransactionTestCase):
    def setUp(self):
        models.PriceEstimate.objects.filter(
            scope=structure_factories.CustomerFactory()
        ).update(total=200)
        models.PriceEstimate.objects.filter(
            scope=structure_factories.CustomerFactory()
        ).update(total=100)
        models.PriceEstimate.objects.filter(
            scope=structure_factories.CustomerFactory()
        ).update(total=300)
        structure_factories.CustomerFactory()

    def execute_request(self, ordering_param=None):
        fixture = structure_fixtures.CustomerFixture()
        url = "/api/financial-reports/"

        self.client.force_login(fixture.staff)
        params = {}
        if ordering_param:
            params[api_settings.ORDERING_PARAM] = ordering_param
        response = self.client.get(url, params)

        return [
            int(customer["billing_price_estimate"]["total"])
            for customer in response.data
        ]

    def test_ascending_ordering(self):
        actual = self.execute_request("estimated_cost")
        self.assertEqual([0, 100, 200, 300], actual)

    def test_descending_ordering(self):
        actual = self.execute_request("-estimated_cost")
        self.assertEqual([300, 200, 100, 0], actual)

    @unittest.skip("Not stable in GitLab CI")
    def test_default_ordering(self):
        actual = self.execute_request()
        self.assertEqual([200, 100, 300, 0], actual)


class CustomerTotalCostFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.prices = [200, 100, 300, 0]
        customers = structure_factories.CustomerFactory.create_batch(len(self.prices))
        for customer, price in zip(customers, self.prices):
            if price == 0:
                continue
            project = structure_factories.ProjectFactory(customer=customer)
            invoice = invoice_factories.InvoiceFactory(customer=customer)
            invoice_factories.InvoiceItemFactory(
                invoice=invoice,
                project=project,
                unit_price=price,
                quantity=1,
                unit=Units.QUANTITY,
            )

    def execute_request(self, ordering_param=None):
        fixture = structure_fixtures.CustomerFixture()
        url = "/api/financial-reports/"

        self.client.force_login(fixture.staff)
        params = {}
        if ordering_param:
            params[api_settings.ORDERING_PARAM] = ordering_param
        response = self.client.get(url, params)

        return [
            int(customer["billing_price_estimate"]["current"])
            for customer in response.data
        ]

    def test_ascending_ordering(self):
        actual = self.execute_request("total_cost")
        self.assertEqual([0, 100, 200, 300], actual)

    def test_descending_ordering(self):
        actual = self.execute_request("-total_cost")
        self.assertEqual([300, 200, 100, 0], actual)

    @unittest.skip("Not stable in GitLab CI")
    def test_default_ordering(self):
        actual = self.execute_request()
        self.assertEqual(self.prices, actual)


@override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
class FinancialReportFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = "/api/financial-reports/"

    def test_if_accounting_start_date_is_none(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, {"accounting_is_running": True})
        self.assertEqual(len(response.data), 0)
        response = self.client.get(self.url, {"accounting_is_running": False})
        self.assertEqual(len(response.data), 0)

    def test_if_accounting_start_date_is_not_none(self):
        self.client.force_authenticate(user=self.fixture.staff)
        self.fixture.customer.accounting_start_date = (
            timezone.now() + datetime.timedelta(days=10)
        )
        response = self.client.get(self.url, {"accounting_is_running": True})
        self.assertEqual(len(response.data), 1)
        response = self.client.get(self.url, {"accounting_is_running": False})
        self.assertEqual(len(response.data), 0)


class FinancialReportProviderFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = "/api/financial-reports/"
        self.customer = self.fixture.customer

        self.provider = structure_factories.CustomerFactory()
        self.another_provider = structure_factories.CustomerFactory()
        self.provider_offering = marketplace_factories.OfferingFactory(
            customer=self.provider
        )
        self.another_provider_offering = marketplace_factories.OfferingFactory(
            customer=self.another_provider
        )
        self.provider_resource = marketplace_factories.ResourceFactory(
            offering=self.provider_offering
        )
        self.another_provider_resource = marketplace_factories.ResourceFactory(
            offering=self.another_provider_offering
        )

        self.invoice = invoice_factories.InvoiceFactory(customer=self.customer)
        invoice_factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=self.provider_resource,
            unit_price=100,
            quantity=2,
            unit=Units.QUANTITY,
        )
        invoice_factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=self.provider_resource,
            unit_price=50,
            quantity=3,
            unit=Units.QUANTITY,
        )

        invoice_factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=self.another_provider_resource,
            unit_price=30,
            quantity=4,
            unit=Units.QUANTITY,
        )

    def get_billing_price_estimate(self, provider_uuid: str) -> dict:
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, {"provider_uuid": provider_uuid})
        self.assertEqual(response.status_code, 200)

        customer_data = next(
            item for item in response.data if item["uuid"] == self.customer.uuid.hex
        )
        return customer_data["billing_price_estimate"]

    def test_filter_by_provider_uuid(self):
        result = self.get_billing_price_estimate(self.provider.uuid.hex)
        self.assertEqual(result["current"], 350)  # 100*2 + 50*3
        self.assertEqual(result["total"], 350)

    def test_filter_by_another_provider_uuid(self):
        result = self.get_billing_price_estimate(self.another_provider.uuid.hex)
        self.assertEqual(result["current"], 120)  # 30*4
        self.assertEqual(result["total"], 120)
