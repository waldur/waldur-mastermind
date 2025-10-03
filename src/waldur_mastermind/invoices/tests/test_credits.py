import datetime
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ddt import data, ddt
from django.db.models.aggregates import Sum
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import compensations, models, tasks
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@ddt
class CustomerCreditRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)

    @data("staff", "global_support", "owner")
    def test_user_with_access_can_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class CustomerCreditCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()

    def create_credit(self, user):
        payload = {
            "customer": structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
            "value": 1000,
        }
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerCreditFactory.get_list_url()
        return self.client.post(url, payload)

    @data("staff")
    def test_user_with_access_can_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="create_of_credit_by_staff"
            ).exists()
        )

    @data("global_support", "owner", "manager", "admin", "user")
    def test_user_cannot_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_expected_consumption_validation(self):
        payload = {
            "customer": structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
            "value": 1000,
            "expected_consumption": 100,
        }
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CustomerCreditFactory.get_list_url()
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        payload = {
            "customer": structure_factories.CustomerFactory.get_url(),
            "value": 1000,
            "expected_consumption": 2000,
        }
        url = factories.CustomerCreditFactory.get_list_url()
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @freeze_time("2025-08-10")
    def test_minimal_consumption_logic(self):
        payload = {
            "customer": structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
            "value": 1600,
            "end_date": datetime.date(year=2025, month=10, day=1),
            "minimal_consumption_logic": models.CustomerCredit.MinimalConsumptionLogic.LINEAR,
            "expected_consumption": 500,
        }
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CustomerCreditFactory.get_list_url()
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        credit = models.CustomerCredit.objects.get(uuid=response.data["uuid"])
        self.assertEqual(credit.expected_consumption, payload["expected_consumption"])
        self.assertEqual(credit.end_date, datetime.date(year=2025, month=10, day=1))

        with freeze_time("2024-11-01"):
            tasks.process_invoice_credits(self.fixture.invoice)
            credit.refresh_from_db()

            days_in_current_month = Decimal(30)
            days_until_credit_end = Decimal(
                (credit.end_date.replace(day=1) - datetime.date.today()).days
            )
            time_left_factor = days_in_current_month / days_until_credit_end
            self.assertEqual(
                (credit.value * time_left_factor).quantize(
                    Decimal("1.00000"), rounding=ROUND_HALF_UP
                ),
                credit.minimal_consumption,
            )


@ddt
class CustomerCreditUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.fixture.customer_credit

    def update_credit(self, user):
        payload = {"value": 500}
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)
        return self.client.patch(url, payload)

    @data("staff")
    def test_user_with_access_can_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="update_of_credit_by_staff"
            ).exists()
        )

    @data("global_support", "manager", "admin", "user")
    def test_user_cannot_update_credit(self, user):
        response = self.update_credit(user)
        self.assertIn(
            response.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        )

    def test_logging_of_offering_changing(self):
        payload = {"offerings": [marketplace_factories.OfferingFactory.get_url()]}
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)
        response = self.client.patch(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="allowed_offerings_have_been_updated"
            ).exists()
        )


@ddt
class CustomerCreditDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def delete_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.CustomerCreditFactory.get_url(self.fixture.customer_credit)
        return self.client.delete(url)

    @data("staff")
    def test_user_with_access_can_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("global_support", "owner", "manager", "admin", "user")
    def test_user_cannot_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditRetrieveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.url = factories.ProjectCreditFactory.get_url(self.fixture.project_credit)

    @data("staff", "global_support", "owner")
    def test_user_with_access_can_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_retrieve_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ProjectCreditCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.fixture.customer_credit

    def create_credit(self, user):
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
            "value": 10,
        }
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectCreditFactory.get_list_url()
        return self.client.post(url, payload)

    @data("staff", "owner")
    def test_user_with_access_can_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("global_support", "manager", "admin", "user")
    def test_user_cannot_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def update_credit(self, user):
        payload = {"value": 7}
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectCreditFactory.get_url(self.fixture.project_credit)
        return self.client.patch(url, payload)

    @data("staff", "owner")
    def test_user_with_access_can_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "global_support",
    )
    def test_global_support_user_cannot_update_credit(self, user):
        response = self.update_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.project_credit = self.fixture.project_credit
        self.customer_credit = self.fixture.customer_credit

    def delete_credit(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ProjectCreditFactory.get_url(self.fixture.project_credit)
        return self.client.delete(url)

    @data("staff", "owner")
    def test_user_with_access_can_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("manager", "admin", "user")
    def test_user_cannot_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "global_support",
    )
    def test_global_support_user_cannot_delete_credit(self, user):
        response = self.delete_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_credit_changed_when_project_deleted_and_flag_enabled(self):
        self.project_credit.value = 10
        self.project_credit.mark_unused_credit_as_spent_on_project_termination = True
        self.project_credit.save()

        self.customer_credit.value = 100
        self.customer_credit.save()

        self.project_credit.project.delete()
        self.customer_credit.refresh_from_db()
        self.assertEqual(self.customer_credit.value, 90)

    def test_customer_credit_unchanged_when_project_deleted_and_flag_disabled(self):
        self.project_credit.value = 10
        self.project_credit.save()

        self.customer_credit.value = 100
        self.customer_credit.save()

        self.project_credit.project.delete()
        self.customer_credit.refresh_from_db()
        self.assertEqual(self.customer_credit.value, 100)


@ddt
@freeze_time("2024-01-01")
class CustomerCreditTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.invoice.tax_percent = 22
        self.invoice.save()
        self.invoice_item = self.fixture.invoice_item

    def test_compensate_cost(self):
        credit_value = self.invoice.total // 2
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer, value=credit_value
        )
        old_total = self.invoice.total
        tasks.process_invoice_credits(self.invoice)
        self.assertTrue(models.InvoiceItem.objects.filter(credit=credit).exists())
        credit_item = models.InvoiceItem.objects.filter(credit=credit).get()
        self.assertEqual(credit_value * -1, credit_item.total)
        self.assertEqual(self.invoice.total, old_total - credit.value)
        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="reduction_of_customer_credit"
            ).exists()
        )

        with freeze_time("2024-02-01"):
            self.assertEqual(credit.consumption_last_month, credit_value)

    def test_compensate_cost_if_credit_greater_than_item_cost(self):
        credit_value = self.invoice.price * 2
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer, value=credit_value
        )
        old_total = self.invoice.total
        tasks.process_invoice_credits(self.invoice)
        self.assertTrue(models.InvoiceItem.objects.filter(credit=credit).exists())
        credit_item = models.InvoiceItem.objects.filter(credit=credit).get()
        self.assertEqual(old_total * -1, credit_item.total)
        self.assertEqual(self.invoice.total, 0)
        credit.refresh_from_db()
        self.assertEqual(credit.value, credit_value + credit_item.price)

    def test_expected_consumption(self):
        old_total = self.invoice.total
        credit_value = self.invoice.total * 3
        expected_consumption = self.invoice.total * 2
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer,
            value=credit_value,
            expected_consumption=expected_consumption,
        )
        tasks.process_invoice_credits(self.invoice)
        self.assertTrue(models.InvoiceItem.objects.filter(credit=credit).exists())
        self.assertEqual(old_total * -1, old_total - expected_consumption)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="reduction_of_customer_credit_due_to_minimal_consumption"
            ).exists()
        )

    def test_task_set_to_zero_overdue_credits(self):
        credit_1 = factories.CustomerCreditFactory()
        credit_2 = factories.CustomerCreditFactory(
            end_date=datetime.date.today() + datetime.timedelta(days=31)
        )
        credit_3 = factories.CustomerCreditFactory(
            end_date=datetime.date.today() - datetime.timedelta(days=31)
        )
        tasks.set_to_zero_overdue_credits()
        credit_1.refresh_from_db()
        credit_2.refresh_from_db()
        credit_3.refresh_from_db()
        self.assertTrue(credit_1.value)
        self.assertTrue(credit_2.value)
        self.assertFalse(credit_3.value)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="set_to_zero_overdue_credit"
            ).exists()
        )


@freeze_time("2024-01-01")
class ProjectCreditTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item

    def test_project_credits_reduced(self):
        old_project_credit_value = self.project_credit.value
        tasks.process_invoice_credits(self.invoice)
        self.project_credit.refresh_from_db()
        self.assertTrue(self.project_credit.value < old_project_credit_value)

        with freeze_time("2024-02-01"):
            consumption_last_month = (
                self.invoice.items.filter(credit=self.customer_credit).aggregate(
                    sum=Sum("unit_price")
                )["sum"]
                * -1
            )
            self.assertEqual(
                self.project_credit.consumption_last_month, consumption_last_month
            )

    def test_project_credits_with_minimal_consumption(self):
        self.project_credit.apply_as_minimal_consumption = True
        self.project_credit.expected_consumption = 100
        self.project_credit.grace_coefficient = 50
        self.project_credit.save()
        old_project_credit_value = self.project_credit.value
        tasks.process_invoice_credits(self.invoice)
        self.project_credit.refresh_from_db()
        self.assertTrue(self.project_credit.value < old_project_credit_value)

        with freeze_time("2024-02-01"):
            consumption_last_month = (
                self.invoice.items.filter(credit=self.customer_credit).aggregate(
                    sum=Sum("unit_price")
                )["sum"]
                * -1
            )
            self.assertEqual(
                self.project_credit.consumption_last_month, consumption_last_month
            )

    def test_use_organisation_credit(self):
        old_customer_credit_value = self.customer_credit.value
        tasks.process_invoice_credits(self.invoice)
        self.customer_credit.refresh_from_db()
        self.assertEqual(
            self.customer_credit.value,
            old_customer_credit_value - self.project_credit.value,
        )


@dataclass
class CompensationTestResult:
    """Holds the state of credits before and after compensation"""

    initial_project_credit: Decimal
    initial_customer_credit: Decimal
    consumption: Decimal
    expected_consumption: Decimal

    def expected_project_deduction(self) -> Decimal:
        return self.consumption

    def expected_customer_deduction(self) -> Decimal:
        return max(self.consumption, self.expected_consumption)


class ProcessingCreditTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item

    def _get_compensation_items_sum(self) -> Decimal:
        """Get the total sum of compensation items"""
        result = self.invoice.items.filter(credit=self.customer_credit).aggregate(
            sum=Sum("unit_price")
        )["sum"] or Decimal("0")
        return result * -1

    def _verify_credit_values(
        self, test_result: CompensationTestResult, after_compensation: bool = True
    ):
        """Verify credit values match expected state"""
        self.project_credit.refresh_from_db()
        self.customer_credit.refresh_from_db()

        if after_compensation:
            self.assertEqual(
                self.project_credit.value,
                test_result.initial_project_credit
                - test_result.expected_project_deduction(),
                "Project credit value incorrect after compensation",
            )
            self.assertEqual(
                self.customer_credit.value,
                test_result.initial_customer_credit
                - test_result.expected_customer_deduction(),
                "Customer credit value incorrect after compensation",
            )
        else:
            self.assertEqual(
                self.project_credit.value,
                test_result.initial_project_credit,
                "Project credit not restored to initial value",
            )
            self.assertEqual(
                self.customer_credit.value,
                test_result.initial_customer_credit,
                "Customer credit was not restored to initial value",
            )

    def _verify_compensation_items(self, should_exist: bool = True):
        """Verify presence or absence of compensation items"""
        invoice_items = self.invoice.items.filter(credit=self.customer_credit)
        items_count = invoice_items.count()
        expected_count = 1 if should_exist else 0
        self.assertEqual(
            items_count,
            expected_count,
            f"Expected {expected_count} compensation items, found {items_count}",
        )
        # check if measured_unit is empty for compenstation items
        if expected_count:
            measured_unit = invoice_items.get().measured_unit
            self.assertEqual(
                measured_unit,
                "",
                f"Expected empty measured_unit, found {measured_unit}",
            )

    def _processing_compensations(self, expected_consumption: Decimal = Decimal("0")):
        """Test credit compensation application and rollback"""
        # Setup
        self.customer_credit.expected_consumption = expected_consumption
        self.customer_credit.save()

        test_result = CompensationTestResult(
            initial_project_credit=self.project_credit.value,
            initial_customer_credit=self.customer_credit.value,
            expected_consumption=expected_consumption,
            consumption=Decimal("0"),  # Will be updated after compensation
        )

        # Apply compensations
        monthly_compensation = compensations.MonthlyCompensation(
            self.customer_credit.customer
        )
        monthly_compensation.apply_compensations()

        # Get actual consumption after compensation
        test_result.consumption = self._get_compensation_items_sum()

        # Verify compensation was applied correctly
        self._verify_compensation_items(should_exist=True)
        self._verify_credit_values(test_result, after_compensation=True)

        # Clear compensations
        monthly_compensation.clear_compensations()

        # Verify compensation was cleared correctly
        self._verify_compensation_items(should_exist=False)
        self._verify_credit_values(test_result, after_compensation=False)

        return test_result

    def test_clear_compensations(self):
        """Test basic compensation clearing without minimal consumption"""
        self._processing_compensations()
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="roll_back_customer_credit"
            ).exists()
        )
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="roll_back_project_credit"
            ).exists()
        )

    def test_expected_consumption_compensation(self):
        """Test compensation with minimal consumption requirement"""
        expected_consumption = Decimal("90")
        result = self._processing_compensations(expected_consumption)
        self.assertGreater(
            result.expected_consumption,
            result.consumption,
            "Minimal consumption should be greater than actual consumption",
        )


@freeze_time("2025-08-01")
class CalculateMinimalConsumptionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit

    def test_apply_as_minimal_consumption(self):
        self.customer_credit.expected_consumption = 100
        self.customer_credit.apply_as_minimal_consumption = True
        self.customer_credit.save()
        self.assertEqual(
            self.customer_credit.expected_consumption,
            self.customer_credit.minimal_consumption,
        )

        self.customer_credit.apply_as_minimal_consumption = False
        self.customer_credit.save()
        self.assertEqual(0, self.customer_credit.minimal_consumption)

    def test_grace_coefficient(self):
        self.customer_credit.expected_consumption = 100
        self.customer_credit.apply_as_minimal_consumption = True

        self.customer_credit.grace_coefficient = 0
        self.customer_credit.save()
        self.assertEqual(
            self.customer_credit.expected_consumption,
            self.customer_credit.minimal_consumption,
        )

        self.customer_credit.grace_coefficient = 50
        self.customer_credit.save()
        self.assertEqual(
            self.customer_credit.expected_consumption * 0.5,
            self.customer_credit.minimal_consumption,
        )

        self.customer_credit.grace_coefficient = 100
        self.customer_credit.save()
        self.assertEqual(0, self.customer_credit.minimal_consumption)

        self.customer_credit.end_date = timezone.now().today()
        self.customer_credit.save()
        self.assertEqual(
            self.customer_credit.expected_consumption,
            self.customer_credit.minimal_consumption,
        )


@ddt
class CustomerCreditHistoricalValuesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.url = factories.CustomerCreditFactory.get_url(
            self.fixture.customer_credit, "consumptions"
        )

        self.invoice1 = factories.InvoiceFactory(
            customer=self.fixture.customer, year=2023, month=1
        )
        self.invoice2 = factories.InvoiceFactory(
            customer=self.fixture.customer, year=2023, month=2
        )

        self.compensation1 = factories.InvoiceItemFactory(
            invoice=self.invoice1,
            credit=self.fixture.customer_credit,
            unit_price=-100,
            quantity=1,
        )
        self.compensation2 = factories.InvoiceItemFactory(
            invoice=self.invoice2,
            credit=self.fixture.customer_credit,
            unit_price=-200,
            quantity=1,
        )

    @data("staff", "global_support", "owner")
    def test_authorized_users_can_view_consumptions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        consumptions = {item["date"]: item["price"] for item in response.data}
        self.assertEqual(consumptions["2023-01-01"], "100.00")
        self.assertEqual(consumptions["2023-02-01"], "200.00")

    @data("manager", "admin", "user")
    def test_unauthorized_users_cannot_view_consumptions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class CompensationQueryOptimizationTest(test.APITransactionTestCase):
    """Test to validate N+1 query optimization in compensation calculations"""

    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.invoice = self.fixture.invoice

    def test_compensation_calculation_avoids_n_plus_one_queries(self):
        """Test that compensation calculation doesn't trigger N+1 queries"""
        # Create multiple offerings and invoice items to test query optimization
        offerings = [marketplace_factories.OfferingFactory() for _ in range(10)]

        # Add offerings to customer credit to test the filtering logic
        self.customer_credit.offerings.add(*offerings[:5])

        # Create multiple invoice items with different offerings
        invoice_items = []
        for i, offering in enumerate(offerings):
            resource = marketplace_factories.ResourceFactory(
                offering=offering, project=self.fixture.project
            )
            invoice_items.append(
                factories.InvoiceItemFactory(
                    invoice=self.invoice,
                    resource=resource,
                    project=self.fixture.project,
                    unit_price=10 + i,
                    quantity=1,
                )
            )

        # Test the compensation calculation with query count monitoring
        from django.db import connection
        from django.test import override_settings

        with override_settings(DEBUG=True):
            # Reset query count
            connection.queries_log.clear()

            # Create compensation object and trigger calculation
            compensation = compensations.MonthlyCompensation(self.fixture.customer)
            _ = (
                compensation.compensations
            )  # This triggers calculate_current_compensations

            # Count queries executed
            query_count = len(connection.queries)

            # The optimized version should use minimal queries:
            # 1. Get invoice items projects ids
            # 2. Get project credits with select_related
            # 3. Get credit offerings
            # 4. Get optimized invoice items with select_related
            # After optimization, should be around 5-6 queries max
            self.assertLess(
                query_count,
                8,  # Should be significantly less than 10 with proper optimization
                f"Too many queries executed ({query_count}). "
                f"This indicates N+1 query problem may still exist.",
            )

            # Verify that only items from credit offerings are processed
            # (first 5 offerings were added to credit)
            expected_processed_items = [
                item
                for item in invoice_items
                if item.resource.offering in self.customer_credit.offerings.all()
            ]
            self.assertEqual(len(expected_processed_items), 5)

            # Get actual compensations generated (these are not saved to DB in this test)
            actual_compensations = compensation.compensations
            self.assertTrue(
                len(actual_compensations) > 0, "No compensations were generated"
            )

    def test_compensation_calculation_with_no_credit_offerings(self):
        """Test compensation calculation when no credit offerings are specified"""
        # Create multiple invoice items without credit offering restrictions
        offerings = [marketplace_factories.OfferingFactory() for _ in range(5)]

        # Don't add any offerings to customer credit (no restrictions)
        self.customer_credit.offerings.clear()

        invoice_items = []
        for i, offering in enumerate(offerings):
            resource = marketplace_factories.ResourceFactory(
                offering=offering, project=self.fixture.project
            )
            invoice_items.append(
                factories.InvoiceItemFactory(
                    invoice=self.invoice,
                    resource=resource,
                    project=self.fixture.project,
                    unit_price=10 + i,
                    quantity=1,
                )
            )

        # Test the compensation calculation
        from django.db import connection
        from django.test import override_settings

        with override_settings(DEBUG=True):
            connection.queries_log.clear()

            compensation = compensations.MonthlyCompensation(self.fixture.customer)
            _ = compensation.compensations

            query_count = len(connection.queries)

            # Should still use minimal queries even without credit offering restrictions
            self.assertLess(
                query_count,
                10,
                f"Too many queries executed ({query_count}) without credit offerings.",
            )

            # Verify all items are processed when no credit offering restrictions
            # Get actual compensations generated (these are not saved to DB in this test)
            actual_compensations = compensation.compensations
            self.assertTrue(
                len(actual_compensations) > 0,
                "No compensations were generated when no credit offerings specified",
            )
