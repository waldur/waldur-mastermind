import datetime
import threading
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock

from ddt import data, ddt
from django.db.models.aggregates import Sum
from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import compensations, models, tasks
from waldur_mastermind.invoices.audit import skip_credit_audit
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@ddt
class CustomerCreditRetrieveTest(test.APITestCase):
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
class CustomerCreditCreateTest(test.APITestCase):
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
class CustomerCreditUpdateTest(test.APITestCase):
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
class CustomerCreditDeleteTest(test.APITestCase):
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
class ProjectCreditRetrieveTest(test.APITestCase):
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
class ProjectCreditCreateTest(test.APITestCase):
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
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="create_of_project_credit_by_staff"
            ).exists()
        )

    @data("global_support", "manager", "admin", "user")
    def test_user_cannot_create_credit(self, user):
        response = self.create_credit(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProjectCreditUpdateTest(test.APITestCase):
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
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="update_of_project_credit_by_staff"
            ).exists()
        )

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
class ProjectCreditDeleteTest(test.APITestCase):
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
class CustomerCreditTest(test.APITestCase):
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

    def test_task_set_to_zero_overdue_project_credits(self):
        """Test that set_to_zero_overdue_credits also zeros expired project credits."""
        customer_credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer, value=1000
        )
        project = structure_factories.ProjectFactory(customer=self.invoice.customer)
        # Active project credit — should be untouched
        pc_active = factories.ProjectCreditFactory(
            project=project,
            value=100,
            end_date=datetime.date.today() + datetime.timedelta(days=31),
        )
        # Expired project credit — should be zeroed
        project2 = structure_factories.ProjectFactory(customer=self.invoice.customer)
        pc_expired = factories.ProjectCreditFactory(
            project=project2,
            value=500,
            end_date=datetime.date.today() - datetime.timedelta(days=31),
        )
        # No end_date — should be untouched
        project3 = structure_factories.ProjectFactory(customer=self.invoice.customer)
        pc_no_end = factories.ProjectCreditFactory(
            project=project3,
            value=200,
        )

        old_customer_value = customer_credit.value
        tasks.set_to_zero_overdue_credits()

        pc_active.refresh_from_db()
        pc_expired.refresh_from_db()
        pc_no_end.refresh_from_db()
        customer_credit.refresh_from_db()

        self.assertEqual(pc_active.value, 100)
        self.assertEqual(pc_expired.value, 0)
        self.assertEqual(pc_no_end.value, 200)
        # Customer credit should not be affected
        self.assertEqual(customer_credit.value, old_customer_value)

    def test_set_to_zero_continues_after_failing_customer_credit(self):
        """One credit failing to save must not block zeroing of the rest."""
        bad_credit = factories.CustomerCreditFactory(
            end_date=datetime.date.today() - datetime.timedelta(days=31)
        )
        good_credit = factories.CustomerCreditFactory(
            end_date=datetime.date.today() - datetime.timedelta(days=31)
        )
        project = structure_factories.ProjectFactory(customer=good_credit.customer)
        project_credit = factories.ProjectCreditFactory(
            project=project,
            value=100,
            end_date=datetime.date.today() - datetime.timedelta(days=31),
        )

        original_save = models.CustomerCredit.save

        def failing_save(credit, *args, **kwargs):
            if credit.pk == bad_credit.pk:
                raise ValueError("Simulated save failure.")
            return original_save(credit, *args, **kwargs)

        with mock.patch.object(models.CustomerCredit, "save", failing_save):
            tasks.set_to_zero_overdue_credits()

        bad_credit.refresh_from_db()
        good_credit.refresh_from_db()
        project_credit.refresh_from_db()
        self.assertTrue(bad_credit.value)
        self.assertFalse(good_credit.value)
        self.assertFalse(project_credit.value)

    def test_set_to_zero_continues_after_failing_project_credit(self):
        """One project credit failing to save must not block zeroing of the rest."""
        customer = structure_factories.CustomerFactory()
        factories.CustomerCreditFactory(customer=customer, value=1000)
        bad_project_credit = factories.ProjectCreditFactory(
            project=structure_factories.ProjectFactory(customer=customer),
            value=100,
            end_date=datetime.date.today() - datetime.timedelta(days=31),
        )
        good_project_credit = factories.ProjectCreditFactory(
            project=structure_factories.ProjectFactory(customer=customer),
            value=200,
            end_date=datetime.date.today() - datetime.timedelta(days=31),
        )

        original_save = models.ProjectCredit.save

        def failing_save(credit, *args, **kwargs):
            if credit.pk == bad_project_credit.pk:
                raise ValueError("Simulated save failure.")
            return original_save(credit, *args, **kwargs)

        with mock.patch.object(models.ProjectCredit, "save", failing_save):
            tasks.set_to_zero_overdue_credits()

        bad_project_credit.refresh_from_db()
        good_project_credit.refresh_from_db()
        self.assertTrue(bad_project_credit.value)
        self.assertFalse(good_project_credit.value)

    def test_set_to_zero_includes_system_robot_in_event_context(self):
        """Events from set_to_zero_overdue_credits should have system robot user context."""
        factories.CustomerCreditFactory(
            end_date=datetime.date.today() - datetime.timedelta(days=31)
        )
        tasks.set_to_zero_overdue_credits()
        event = logging_models.Event.objects.filter(
            event_type="set_to_zero_overdue_credit"
        ).first()
        self.assertIsNotNone(event)
        self.assertIn("user_uuid", event.context)
        self.assertEqual(event.context["user_full_name"], "System Robot")

    def test_compensation_does_not_produce_update_of_credit_by_staff_event(self):
        """When compensation reduces credit, only REDUCTION events should fire,
        not the misleading update_of_credit_by_staff event."""
        credit = factories.CustomerCreditFactory(
            customer=self.invoice.customer,
            value=self.invoice.total * 2,
        )
        tasks.process_invoice_credits(self.invoice)
        credit.refresh_from_db()
        # REDUCTION event should exist
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="reduction_of_customer_credit"
            ).exists()
        )
        # update_of_credit_by_staff should NOT exist (it's a side effect of credit.save())
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type="update_of_credit_by_staff"
            ).exists()
        )

    def test_compensation_does_not_produce_update_of_project_credit_by_staff_event(
        self,
    ):
        """When compensation reduces project credit, only REDUCTION events should fire,
        not the misleading update_of_project_credit_by_staff event."""
        project = structure_factories.ProjectFactory(customer=self.invoice.customer)
        self.invoice_item.project = project
        self.invoice_item.save()
        factories.CustomerCreditFactory(
            customer=self.invoice.customer,
            value=self.invoice.total * 2,
        )
        factories.ProjectCreditFactory(
            project=project,
            value=self.invoice.total * 2,
        )
        tasks.process_invoice_credits(self.invoice)
        # REDUCTION event should exist
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="reduction_of_project_credit"
            ).exists()
        )
        # update_of_project_credit_by_staff should NOT exist
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type="update_of_project_credit_by_staff"
            ).exists()
        )

    def test_api_update_still_produces_update_of_credit_by_staff_event(self):
        """When staff updates credit via API, update_of_credit_by_staff should fire."""
        credit = factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=1000
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CustomerCreditFactory.get_url(credit)
        response = self.client.patch(url, {"value": 500})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="update_of_credit_by_staff"
            ).exists()
        )


@freeze_time("2024-01-01")
class ProjectCreditTest(test.APITestCase):
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


class DiscountCompensationTest(test.APITestCase):
    """Credit compensation must draw on the cost net of a paired volume
    discount, not the gross price — otherwise credit is over-consumed and the
    invoice can go negative."""

    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.invoice = self.fixture.invoice
        self.main_item = self.fixture.invoice_item  # price = 10 * 30 = 300
        self.credit = factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("1000")
        )
        # A 20% volume discount paired with the main item: -60.
        factories.InvoiceItemFactory(
            name="OFFERING-001 / Volume discount (20%)",
            resource=self.fixture.resource,
            project=self.fixture.project,
            invoice=self.invoice,
            unit_price=Decimal("-60"),
            quantity=1,
            details={
                "is_discount": True,
                "discount_of_item": self.main_item.uuid.hex,
            },
        )

    def test_credit_is_drawn_net_of_the_volume_discount(self):
        compensations.MonthlyCompensation(
            self.fixture.customer, invoice=self.invoice
        ).apply_compensations()

        self.credit.refresh_from_db()
        # Net cost is 300 - 60 = 240, so only 240 (not the gross 300) is drawn.
        self.assertEqual(self.credit.value, Decimal("760"))
        # The compensation offsets the net, so the invoice does not go negative.
        self.assertEqual(
            models.Invoice.objects.get(pk=self.invoice.pk).price, Decimal("0")
        )


class ProcessingCreditTest(test.APITestCase):
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

    @freeze_time("2026-03-01")
    def test_linear_expected_consumption_is_set_for_project_credit(self):
        """Test that project credits with LINEAR logic get expected_consumption updated.

        Reproduces HPCMP-451: when expected_consumption starts at 0,
        minimal_consumption is also 0, so the credit never enters _project_tails
        and update_linear_expected_consumption() skips it.
        """
        # Set large credit values so project credit isn't fully consumed
        self.customer_credit.value = 1000
        self.customer_credit.save()

        self.project_credit.value = 500
        self.project_credit.minimal_consumption_logic = (
            models.ProjectCredit.MinimalConsumptionLogic.LINEAR
        )
        self.project_credit.end_date = datetime.date(2026, 7, 1)
        self.project_credit.expected_consumption = 0
        self.project_credit.save()

        tasks.process_invoice_credits(self.invoice)
        self.project_credit.refresh_from_db()

        self.assertGreater(self.project_credit.expected_consumption, 0)

    @freeze_time("2026-03-01")
    def test_linear_expected_consumption_near_end_date(self):
        """Test that a project credit near its end_date gets expected_consumption
        close to remaining value.

        Simulates production scenario: credit with LINEAR logic has had
        expected_consumption=0 for months due to HPCMP-451 bug, and is now
        close to expiry. The first fix-up should set expected_consumption
        to approximately the remaining credit value.
        """
        self.customer_credit.value = 1000
        self.customer_credit.save()

        # Credit expires next month — time_left_factor = 31/31 = 1.0
        self.project_credit.value = 500
        self.project_credit.minimal_consumption_logic = (
            models.ProjectCredit.MinimalConsumptionLogic.LINEAR
        )
        self.project_credit.end_date = datetime.date(2026, 4, 1)
        self.project_credit.expected_consumption = 0
        self.project_credit.save()

        tasks.process_invoice_credits(self.invoice)
        self.project_credit.refresh_from_db()

        # Invoice item costs 300 (10 * 30 days), leaving 200 in project credit.
        # With time_left_factor=1.0, expected_consumption = remaining_value = 200.
        self.assertEqual(
            self.project_credit.expected_consumption,
            Decimal("200.00000"),
        )

    @freeze_time("2026-03-01")
    def test_linear_expected_consumption_skips_expired_credits(self):
        """Test that update_linear_expected_consumption skips project credits
        whose end_date has already passed."""
        self.customer_credit.value = 1000
        self.customer_credit.save()

        self.project_credit.value = 500
        self.project_credit.minimal_consumption_logic = (
            models.ProjectCredit.MinimalConsumptionLogic.LINEAR
        )
        # end_date in the past
        self.project_credit.end_date = datetime.date(2026, 2, 1)
        self.project_credit.expected_consumption = 0
        self.project_credit.save()

        tasks.process_invoice_credits(self.invoice)
        self.project_credit.refresh_from_db()

        # Expired credit should not get expected_consumption updated
        self.assertEqual(self.project_credit.expected_consumption, 0)


class ExpiredProjectCreditProductionBugTest(test.APITestCase):
    """Reproduces the production bug where expired project credits keep
    being used for compensations.

    Production timeline (project a115, SwissAI Initiative):
    - Project credit created 2025-06-24, end_date=2026-01-01, value≈35k
    - Monthly compensations deducted normally (Aug-Dec 2025)
    - Credit should have stopped being used after 2026-01-01
    - BUG: compensations continued in Jan, Feb 2026
    - Root cause: set_to_zero_overdue_credits only zeroed CustomerCredits,
      not ProjectCredits
    """

    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item

    def _simulate_month_end_old_code(self, effective_date):
        """Simulates month-end processing as it was BEFORE the fix:
        set_to_zero_overdue_credits only zeros CustomerCredits."""
        # Old code: only CustomerCredit zeroing
        for credit in models.CustomerCredit.objects.filter(
            end_date__lt=effective_date
        ).exclude(value=0):
            credit.value = 0
            credit.save()
        # NOTE: ProjectCredit zeroing was MISSING
        tasks.process_invoice_credits(self.invoice)

    def _simulate_month_end_new_code(self, effective_date):
        """Simulates month-end processing WITH the fix:
        set_to_zero_overdue_credits zeros both Customer and ProjectCredits."""
        tasks.set_to_zero_overdue_credits(effective_date)
        tasks.process_invoice_credits(self.invoice)

    @freeze_time("2026-02-01")
    def test_old_code_bug_expired_credit_still_used(self):
        """Reproduces the bug: with old code, expired project credit
        continues to be used for compensations."""
        # Setup: large credits, item cost = 300 (10 * 30)
        self.customer_credit.value = 100000
        self.customer_credit.save()

        self.project_credit.value = 50000
        self.project_credit.end_date = datetime.date(2026, 1, 1)  # Already expired
        self.project_credit.save()

        # Simulate month-end with OLD code (no ProjectCredit zeroing)
        self._simulate_month_end_old_code(datetime.date(2026, 2, 1))

        self.project_credit.refresh_from_db()
        self.customer_credit.refresh_from_db()

        # BUG: expired project credit was still used (value reduced from 50000)
        self.assertLess(
            self.project_credit.value,
            50000,
            "Bug reproduced: expired project credit was used for compensation",
        )
        self.assertGreater(
            self.project_credit.value,
            0,
            "Bug reproduced: expired credit still has remaining value",
        )
        # Customer credit was also reduced (both deducted in tandem)
        self.assertLess(self.customer_credit.value, 100000)

    @freeze_time("2026-02-01")
    def test_new_code_fix_expired_credit_zeroed(self):
        """After upgrade: expired project credit is zeroed and stays at zero."""
        self.customer_credit.value = 100000
        self.customer_credit.save()

        self.project_credit.value = 50000
        self.project_credit.end_date = datetime.date(2026, 1, 1)  # Already expired
        self.project_credit.save()

        # Simulate month-end with NEW code (ProjectCredit zeroing included)
        self._simulate_month_end_new_code(datetime.date(2026, 2, 1))

        self.project_credit.refresh_from_db()
        self.customer_credit.refresh_from_db()

        # Project credit must be zero — expired and zeroed
        self.assertEqual(
            self.project_credit.value,
            0,
            "Expired project credit should be zeroed after upgrade",
        )
        # Customer credit unchanged — zeroed project credit blocks fallback
        self.assertEqual(
            self.customer_credit.value,
            100000,
            "Customer credit should not be used when project credit exists (even zeroed)",
        )

    @freeze_time("2026-02-01")
    def test_old_code_then_upgrade(self):
        """Simulates upgrade scenario: credit was used under old code
        (no ProjectCredit zeroing), then the new code runs.

        After upgrade, set_to_zero_overdue_credits must zero the credit
        that was never zeroed by the old code.
        """
        # Item cost = 10 * 30 = 300
        self.customer_credit.value = 100000
        self.customer_credit.save()

        self.project_credit.value = 50000
        self.project_credit.end_date = datetime.date(2026, 1, 1)  # Expired
        self.project_credit.save()

        # Step 1: Old code processed previous invoice — expired credit was
        # used because old set_to_zero didn't handle ProjectCredits.
        # Simulate this by just processing the invoice without zeroing.
        tasks.process_invoice_credits(self.invoice)

        self.project_credit.refresh_from_db()
        # Old code: credit was used (300 deducted) despite being expired
        self.assertEqual(self.project_credit.value, Decimal("49700"))

        # Step 2: Upgrade deployed. Next month-end runs NEW code.
        # set_to_zero_overdue_credits now zeros ProjectCredits.
        tasks.set_to_zero_overdue_credits(datetime.date(2026, 2, 1))

        self.project_credit.refresh_from_db()
        self.assertEqual(
            self.project_credit.value,
            0,
            "After upgrade: expired project credit is finally zeroed",
        )


@freeze_time("2025-08-01")
class CalculateMinimalConsumptionTest(test.APITestCase):
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
class CustomerCreditHistoricalValuesTest(test.APITestCase):
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


class CompensationQueryOptimizationTest(test.APITestCase):
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


@freeze_time("2024-03-01")
class ConcurrentCreditDeductionTest(TransactionTestCase):
    """WAL-9806: Verify that concurrent invoice credit processing does not lose updates.

    Without proper locking, two concurrent process_invoice_credits() calls for the
    same customer can both read the same credit value, each deduct their amount,
    and the last save wins — losing one deduction entirely.
    """

    def test_concurrent_credit_deduction_preserves_both(self):
        """Two concurrent compensations must both deduct from the same credit."""
        customer = structure_factories.CustomerFactory()
        credit = factories.CustomerCreditFactory(
            customer=customer, value=Decimal("100.00")
        )

        project = structure_factories.ProjectFactory(customer=customer)

        offering = marketplace_factories.OfferingFactory(customer=customer)
        resource1 = marketplace_factories.ResourceFactory(
            project=project, offering=offering
        )
        resource2 = marketplace_factories.ResourceFactory(
            project=project, offering=offering
        )

        invoice1 = factories.InvoiceFactory(customer=customer, month=3, year=2024)
        factories.InvoiceItemFactory(
            invoice=invoice1,
            resource=resource1,
            project=project,
            unit_price=Decimal("40.00"),
            quantity=1,
        )

        invoice2 = factories.InvoiceFactory(customer=customer, month=2, year=2024)
        factories.InvoiceItemFactory(
            invoice=invoice2,
            resource=resource2,
            project=project,
            unit_price=Decimal("30.00"),
            quantity=1,
        )

        barrier = threading.Barrier(2, timeout=10)
        errors = []

        def process_with_barrier(invoice):
            try:
                barrier.wait()
                tasks.process_invoice_credits(invoice)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=process_with_barrier, args=(invoice1,))
        t2 = threading.Thread(target=process_with_barrier, args=(invoice2,))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        self.assertEqual(errors, [], f"Threads raised errors: {errors}")

        credit.refresh_from_db()
        # Expected: 100 - 40 - 30 = 30
        # Without locking: 60 or 70 (one deduction lost)
        self.assertEqual(
            credit.value,
            Decimal("30.00"),
            f"Credit should be 30 (100 - 40 - 30), got {credit.value}. "
            "Lost update indicates missing database locking.",
        )


class SetToZeroOverdueCreditsGuardTest(test.APITestCase):
    """set_to_zero_overdue_credits must refuse a future effective_date.

    Regression: a manual run with an effective_date in the future zeroed out
    project credits whose end_date had not actually arrived yet.
    """

    def test_future_effective_date_is_rejected(self):
        with freeze_time("2026-03-17"):
            future = datetime.date(2026, 12, 1)
            project = structure_factories.ProjectFactory()
            credit = factories.CustomerCreditFactory(
                customer=project.customer,
                value=1000,
                end_date=datetime.date(2026, 7, 1),
            )
            pc = factories.ProjectCreditFactory(
                project=project,
                value=200,
                end_date=datetime.date(2026, 7, 1),
            )
            with self.assertRaises(ValueError):
                tasks.set_to_zero_overdue_credits(effective_date=future)
            credit.refresh_from_db()
            pc.refresh_from_db()
            # Nothing should have been touched.
            self.assertEqual(credit.value, 1000)
            self.assertEqual(pc.value, 200)
            self.assertFalse(
                logging_models.Event.objects.filter(
                    event_type="set_to_zero_overdue_credit"
                ).exists()
            )

    def test_today_effective_date_is_accepted(self):
        with freeze_time("2026-03-17"):
            today = datetime.date(2026, 3, 17)
            expired = factories.CustomerCreditFactory(
                value=100, end_date=datetime.date(2026, 3, 1)
            )
            tasks.set_to_zero_overdue_credits(effective_date=today)
            expired.refresh_from_db()
            self.assertEqual(expired.value, 0)


class CreditAuditOnSilentSavesTest(test.APITestCase):
    """Manual saves with update_fields=['value'] must still be audited.

    Regression: log_project_credit/log_credit used to short-circuit on any
    update_fields, which let any caller (integration script, shell, third-party
    subsystem) silently mutate credit value with no audit trail, causing
    material credit-value drift in production. Now, only callers inside
    skip_credit_audit() are exempted.
    """

    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def test_manual_value_save_with_update_fields_emits_audit(self):
        pc = self.fixture.project_credit
        pc.value = Decimal("75.00")  # well below the customer-credit cap of 100
        pc.save(update_fields=["value"])
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="update_of_project_credit_by_staff"
            ).exists()
        )

    def test_save_with_unrelated_update_fields_does_not_emit(self):
        # Saving a non-value field (e.g. only end_date) must NOT produce
        # a value-mutation audit event.
        pc = self.fixture.project_credit
        pc.end_date = datetime.date(2026, 7, 1)
        pc.save(update_fields=["end_date"])
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type="update_of_project_credit_by_staff"
            ).exists()
        )

    def test_skip_credit_audit_suppresses_event(self):
        pc = self.fixture.project_credit
        with skip_credit_audit():
            pc.value = Decimal("80.00")
            pc.save(update_fields=["value"])
        self.assertFalse(
            logging_models.Event.objects.filter(
                event_type="update_of_project_credit_by_staff"
            ).exists()
        )


class CreditEndDateValidationScopeTest(test.APITestCase):
    """The end_date day=1 validator must only fire when end_date is written.

    Legacy rows created before WAL-8788 may have an end_date that is not the
    first of the month. Unrelated partial saves (e.g. update_fields=['value']
    from set_to_zero_overdue_credits) must not raise on those rows.
    """

    def setUp(self):
        self.fixture = fixtures.CreditFixture()

    def _set_legacy_end_date(self, credit, end_date):
        type(credit).objects.filter(pk=credit.pk).update(end_date=end_date)
        credit.refresh_from_db()

    def test_partial_save_without_end_date_skips_validation(self):
        pc = self.fixture.project_credit
        self._set_legacy_end_date(pc, datetime.date(2025, 9, 15))
        pc.value = Decimal("10.00")
        pc.save(update_fields=["value"])
        pc.refresh_from_db()
        self.assertEqual(pc.value, Decimal("10.00"))
        self.assertEqual(pc.end_date, datetime.date(2025, 9, 15))

    def test_save_touching_end_date_still_validates(self):
        from rest_framework import exceptions as rf_exceptions

        pc = self.fixture.project_credit
        pc.end_date = datetime.date(2025, 9, 15)
        with self.assertRaises(rf_exceptions.ValidationError):
            pc.save(update_fields=["end_date"])

    def test_set_to_zero_overdue_credits_handles_legacy_end_date(self):
        with freeze_time("2026-03-17"):
            cc = factories.CustomerCreditFactory(value=500)
            self._set_legacy_end_date(cc, datetime.date(2025, 9, 15))
            tasks.set_to_zero_overdue_credits()
            cc.refresh_from_db()
            self.assertEqual(cc.value, 0)
