"""A project credit paying the uncovered cost of its project's earlier invoices."""

import datetime
import decimal

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import compensations, models, settlement, tasks
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

AUGUST = datetime.date(2026, 8, 1)
SEPTEMBER = datetime.date(2026, 9, 1)
OCTOBER = datetime.date(2026, 10, 1)


class SettlementTestMixin:
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.customer_credit = factories.CustomerCreditFactory(
            customer=self.customer, value=100000
        )
        self.project_credit = factories.ProjectCreditFactory(
            project=self.project, value=25000, pay_earlier_invoices=True
        )

    def invoice(self, period, state=models.Invoice.States.CREATED):
        invoice, _ = models.Invoice.objects.get_or_create(
            customer=self.customer, year=period.year, month=period.month
        )
        invoice.state = state
        invoice.save(update_fields=["state"])
        return invoice

    def item(self, invoice, price, resource=None, project=None, **kwargs):
        resource = resource or self.resource
        return factories.InvoiceItemFactory(
            invoice=invoice,
            project=project or self.project,
            resource=resource,
            unit_price=decimal.Decimal(price),
            quantity=1,
            **kwargs,
        )

    def payments(self, period=None):
        payments = models.InvoiceItem.objects.filter(compensated_period__isnull=False)
        if period:
            payments = payments.filter(compensated_period=period)
        return payments

    def paid(self, period=None):
        return -sum((p.price for p in self.payments(period)), decimal.Decimal(0))

    def refresh(self):
        self.project_credit.refresh_from_db()
        self.customer_credit.refresh_from_db()

    @staticmethod
    def pay(project_credit):
        payment_plan = settlement.pay_earlier_invoices(project_credit)
        return payment_plan.total if payment_plan else 0


@freeze_time("2026-10-15")
class PayEarlierInvoicesTest(SettlementTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.september = self.invoice(SEPTEMBER)
        self.september_item = self.item(self.september, 20000)
        self.october = self.invoice(OCTOBER, models.Invoice.States.PENDING)
        self.item(self.october, 10)

    def test_pays_the_uncovered_cost_onto_the_open_invoice(self):
        paid = self.pay(self.project_credit)

        self.assertEqual(paid, 20000)
        payment = self.payments().get()
        self.assertEqual(payment.invoice, self.october)
        self.assertEqual(payment.compensated_period, SEPTEMBER)
        self.assertEqual(payment.credit, self.customer_credit)
        self.assertEqual(payment.resource, self.resource)
        self.assertEqual(payment.project_uuid, self.project.uuid.hex)
        self.assertTrue(payment.details["is_compensation"])
        self.assertEqual(
            payment.details["compensation_of_item"], self.september_item.uuid.hex
        )
        self.refresh()
        self.assertEqual(self.project_credit.value, 5000)
        self.assertEqual(self.customer_credit.value, 80000)

    def test_draw_is_in_the_ledger(self):
        settlement.pay_earlier_invoices(self.project_credit)

        rows = models.CreditTransaction.objects.filter(
            transaction_type=models.CreditTransaction.Types.HISTORICAL_COMPENSATION
        )
        self.assertEqual(
            {(r.credit_id, r.project_credit_id, r.amount) for r in rows},
            {
                (self.customer_credit.pk, None, -20000),
                (None, self.project_credit.pk, -20000),
            },
        )
        self.assertEqual({r.billing_period for r in rows}, {OCTOBER})
        # The organization credit's row has no project of its own.
        self.assertEqual(
            {r.comment for r in rows},
            {f"Pays the uncovered cost of project {self.project.name} in 2026-09"},
        )

    def test_payment_events_name_the_item(self):
        settlement.pay_earlier_invoices(self.project_credit)

        payment = self.payments(SEPTEMBER).get()
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.INVOICE_ITEM_CREATED,
                message=f"Invoice item {payment.name} has been created.",
            ).exists()
        )

    def test_paying_again_pays_nothing(self):
        settlement.pay_earlier_invoices(self.project_credit)

        self.assertEqual(self.pay(self.project_credit), 0)
        self.assertEqual(self.paid(), 20000)

    def test_turned_off_pays_nothing(self):
        self.project_credit.pay_earlier_invoices = False
        self.project_credit.save()

        self.assertEqual(self.pay(self.project_credit), 0)
        self.assertFalse(self.payments().exists())

    def test_pays_only_what_is_left_after_this_months_usage(self):
        self.project_credit.value = 5000
        self.project_credit.save()

        self.assertEqual(self.pay(self.project_credit), 4990)

    def test_organization_credit_caps_the_payment(self):
        self.customer_credit.value = 3000
        self.customer_credit.save()

        self.assertEqual(self.pay(self.project_credit), 2990)

    def test_compensation_runs_leave_payments_alone(self):
        settlement.pay_earlier_invoices(self.project_credit)
        run = compensations.MonthlyCompensation(self.customer, invoice=self.october)
        run.apply_compensations()
        compensations.MonthlyCompensation(
            self.customer, invoice=self.october
        ).apply_compensations()

        self.assertEqual(self.paid(SEPTEMBER), 20000)
        self.refresh()
        # 20 000 for September, then October's own 10, once.
        self.assertEqual(self.project_credit.value, 4990)
        self.assertEqual(self.customer_credit.value, 79990)

    def test_compensation_runs_leave_payments_alone_with_a_minimal_consumption(self):
        self.customer_credit.expected_consumption = 500
        self.customer_credit.save()
        settlement.pay_earlier_invoices(self.project_credit)
        for _ in range(2):
            compensations.MonthlyCompensation(
                self.customer, invoice=self.october
            ).apply_compensations()

        self.assertEqual(self.paid(SEPTEMBER), 20000)
        self.customer_credit.refresh_from_db()
        # The floor of 500 is drawn once, on top of the payment.
        self.assertEqual(self.customer_credit.value, 100000 - 20000 - 500)

    def test_month_end_pays_after_the_months_own_compensation(self):
        self.project_credit.value = 15000
        self.project_credit.save()

        tasks.process_invoice_credits(self.october)

        self.refresh()
        self.assertEqual(self.paid(SEPTEMBER), 14990)
        self.assertEqual(self.project_credit.value, 0)
        self.assertEqual(
            -self.october.items.filter(
                credit=self.customer_credit, compensated_period__isnull=True
            )
            .get()
            .price,
            10,
        )

    def test_month_end_does_not_pay_what_was_paid_mid_month(self):
        settlement.pay_earlier_invoices(self.project_credit)

        tasks.process_invoice_credits(self.october)

        self.assertEqual(self.paid(SEPTEMBER), 20000)

    def test_a_credit_not_set_to_pay_is_left_alone_at_month_end(self):
        self.project_credit.pay_earlier_invoices = False
        self.project_credit.save()

        tasks.process_invoice_credits(self.october)

        self.assertFalse(self.payments().exists())

    def test_project_without_items_on_the_open_invoice_pays(self):
        self.october.items.all().delete()

        self.assertEqual(self.pay(self.project_credit), 20000)

    def test_canceled_and_open_invoices_are_not_paid(self):
        august = self.invoice(AUGUST, models.Invoice.States.CANCELED)
        self.item(august, 1000)
        self.september.state = models.Invoice.States.PENDING
        self.september.save()

        self.assertEqual(self.pay(self.project_credit), 0)

    def test_offerings_the_credit_does_not_cover_are_not_paid(self):
        self.customer_credit.offerings.add(marketplace_factories.OfferingFactory())

        self.assertEqual(self.pay(self.project_credit), 0)

    def test_period_bounds_are_honoured(self):
        august = self.invoice(AUGUST)
        self.item(august, 1000)
        self.project_credit.earlier_invoices_until = AUGUST
        self.project_credit.save()

        settlement.pay_earlier_invoices(self.project_credit)
        self.assertEqual(self.paid(AUGUST), 1000)
        self.assertEqual(self.paid(SEPTEMBER), 0)

        self.project_credit.earlier_invoices_from = SEPTEMBER
        self.project_credit.earlier_invoices_until = None
        self.project_credit.save()
        settlement.pay_earlier_invoices(self.project_credit)
        self.assertEqual(self.paid(SEPTEMBER), 20000)

    def test_oldest_month_is_paid_first(self):
        august = self.invoice(AUGUST)
        self.item(august, 1000)
        self.project_credit.value = 1500
        self.project_credit.save()

        settlement.pay_earlier_invoices(self.project_credit)

        self.assertEqual(self.paid(AUGUST), 1000)
        self.assertEqual(self.paid(SEPTEMBER), 490)

    def test_compensation_already_on_the_invoice_counts(self):
        self.item(
            self.september,
            -15000,
            credit=self.customer_credit,
            details={
                "is_compensation": True,
                "compensation_of_item": self.september_item.uuid.hex,
            },
        )

        self.assertEqual(self.pay(self.project_credit), 5000)

    def test_compensation_whose_credit_was_deleted_still_counts(self):
        # Deleting an organization credit nulls `credit` on its compensations.
        self.item(self.september, -20000, credit=None)

        self.assertEqual(self.pay(self.project_credit), 0)

    def test_staff_refund_of_an_item_counts(self):
        # InvoiceItemViewSet.create_compensation writes it on a later invoice.
        self.item(
            self.october,
            -20000,
            details={"original_invoice_item_uuid": self.september_item.uuid.hex},
        )

        self.assertEqual(self.pay(self.project_credit), 0)

    def test_legacy_compensation_without_a_link_is_paid_on_the_expensive_item(self):
        # Compensations from before `compensation_of_item` carry no link. The
        # run paid cheapest first, so what is left sits on the expensive item.
        self.september_item.delete()
        self.item(self.september, 300)
        expensive = self.item(self.september, 700)
        self.item(self.september, -700, credit=self.customer_credit)

        settlement.pay_earlier_invoices(self.project_credit)

        payment = self.payments().get()
        self.assertEqual(payment.price, -300)
        self.assertEqual(payment.details["compensation_of_item"], expensive.uuid.hex)

    def test_moved_resource_does_not_bring_its_history(self):
        other_project = structure_factories.ProjectFactory(customer=self.customer)
        other_credit = factories.ProjectCreditFactory(
            project=other_project, value=25000, pay_earlier_invoices=True
        )
        self.resource.project = other_project
        self.resource.save()

        self.assertEqual(self.pay(other_credit), 0)
        self.assertEqual(self.pay(self.project_credit), 20000)


@freeze_time("2026-10-01 02:00")
class GracePeriodTest(SettlementTestMixin, test.APITestCase):
    """While September waits to be finalized, both it and October are open."""

    def setUp(self):
        super().setUp()
        self.item(self.invoice(AUGUST), 1000)
        self.september = self.invoice(
            SEPTEMBER, models.Invoice.States.PENDING_FINALIZATION
        )
        self.invoice(OCTOBER, models.Invoice.States.PENDING)

    def test_paying_now_then_finalizing_pays_once(self):
        settlement.pay_earlier_invoices(self.project_credit)
        tasks.process_invoice_credits(self.september)

        self.assertEqual(self.paid(AUGUST), 1000)

    def test_finalizing_then_paying_now_pays_once(self):
        tasks.process_invoice_credits(self.september)
        settlement.pay_earlier_invoices(self.project_credit)

        self.assertEqual(self.paid(AUGUST), 1000)


@freeze_time("2026-10-15")
class ProjectCreditEarlierInvoicesApiTest(SettlementTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.project_credit.pay_earlier_invoices = False
        self.project_credit.save()
        self.september = self.invoice(SEPTEMBER)
        self.item(self.september, 20000)
        self.item(self.september, -5000, credit=self.customer_credit)
        self.invoice(OCTOBER, models.Invoice.States.PENDING)
        self.url = factories.ProjectCreditFactory.get_url(self.project_credit)

    def test_staff_turning_it_on_pays_at_once(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.patch(self.url, {"pay_earlier_invoices": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.paid(SEPTEMBER), 15000)
        self.assertEqual(decimal.Decimal(response.data["value"]), 10000)

    def test_raising_the_allocation_later_pays_more(self):
        self.project_credit.value = 5000
        self.project_credit.pay_earlier_invoices = True
        self.project_credit.save()
        settlement.pay_earlier_invoices(self.project_credit)
        self.assertEqual(self.paid(SEPTEMBER), 5000)
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.patch(self.url, {"value": 20000})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.paid(SEPTEMBER), 15000)
        self.assertEqual(decimal.Decimal(response.data["value"]), 10000)

    def test_earlier_invoices_lists_what_credit_left_uncovered(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(self.url + "earlier_invoices/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.json(),
            {
                "months": [
                    {
                        "period": "2026-09-01",
                        "cost": "20000.00",
                        "covered": "5000.00",
                        "uncovered": "15000.00",
                        "in_period": True,
                    }
                ],
                "uncovered": "15000.00",
                "uncovered_in_period": "15000.00",
                "payable_now": "15000.00",
            },
        )

    def test_earlier_invoices_previews_other_bounds(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.url + "earlier_invoices/", {"earlier_invoices_from": "2026-10-01"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(m["period"], m["in_period"]) for m in response.data["months"]],
            [("2026-09-01", False)],
        )
        self.assertEqual(response.data["payable_now"], "0.00")

    def test_earlier_invoices_lists_months_the_credit_does_not_pay(self):
        """A Total cost policy counts every month, so its explanation needs
        them all; the credit's bounds decide only what it pays."""
        self.project_credit.earlier_invoices_until = AUGUST
        self.project_credit.save()
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(self.url + "earlier_invoices/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(m["period"], m["in_period"]) for m in response.data["months"]],
            [("2026-09-01", False)],
        )
        self.assertEqual(response.data["uncovered"], "15000.00")
        self.assertEqual(response.data["uncovered_in_period"], "0.00")
        self.assertEqual(response.data["payable_now"], "0.00")

    def mark_events(self):
        self.event_mark = Event.objects.order_by("-id").values_list("id", flat=True)[0]

    def credit_events(self):
        """The credit audit events since `mark_events`: setting up the credit
        records its own."""
        return list(
            Event.objects.filter(
                event_type=EventType.UPDATE_OF_PROJECT_CREDIT_BY_STAFF,
                id__gt=self.event_mark,
            ).order_by("id")
        )

    def test_turning_it_on_is_audited(self):
        self.client.force_authenticate(self.fixture.staff)
        self.mark_events()

        self.client.patch(
            self.url,
            {
                "pay_earlier_invoices": True,
                "earlier_invoices_from": "2026-07-01",
                "earlier_invoices_compensate_in": "incurred_month",
            },
        )

        [event] = self.credit_events()
        self.assertEqual(
            event.message,
            f"{self.project.name} project credit pays earlier invoices from "
            "2026-07, written on the invoice of the month the cost was incurred.",
        )
        self.assertEqual(event.context["user_username"], self.fixture.staff.username)
        self.assertEqual(
            event.context["earlier_invoices_changes"],
            {
                "pay_earlier_invoices": {"old": False, "new": True},
                "earlier_invoices_from": {"old": None, "new": "2026-07-01"},
                "earlier_invoices_compensate_in": {
                    "old": "current_month",
                    "new": "incurred_month",
                },
            },
        )

    def test_turning_it_off_is_audited(self):
        self.project_credit.pay_earlier_invoices = True
        self.project_credit.save()
        self.client.force_authenticate(self.fixture.staff)
        self.mark_events()

        self.client.patch(self.url, {"pay_earlier_invoices": False})

        [event] = self.credit_events()
        self.assertEqual(
            event.message,
            f"{self.project.name} project credit no longer pays earlier invoices.",
        )

    def test_paying_on_request_is_audited(self):
        self.client.force_authenticate(self.fixture.staff)
        self.mark_events()

        self.client.post(self.url + "pay_earlier_invoices/", {})

        [event] = self.credit_events()
        self.assertEqual(
            event.message,
            f"{self.project.name} project credit pays earlier invoices, written "
            "on the invoice of the current month.",
        )

    def test_a_change_of_value_alone_is_audited_as_before(self):
        self.client.force_authenticate(self.fixture.owner)
        self.mark_events()

        self.client.patch(self.url, {"value": 30000})

        [event] = self.credit_events()
        self.assertEqual(
            event.message,
            f"{self.project.name} project credit has been updated from 25000 to 30000.",
        )

    def test_project_roles_see_earlier_invoices_while_billing_is_displayed(self):
        self.client.force_authenticate(self.fixture.member)

        response = self.client.get(self.url + "earlier_invoices/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_roles_cannot_see_earlier_invoices_when_billing_is_hidden(self):
        self.customer.display_billing_info_in_projects = False
        self.customer.save()
        self.client.force_authenticate(self.fixture.member)

        response = self.client.get(self.url + "earlier_invoices/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_users_outside_the_project_cannot_see_earlier_invoices(self):
        self.client.force_authenticate(self.fixture.user)

        response = self.client.get(self.url + "earlier_invoices/")

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


@freeze_time("2026-10-15")
class PayEarlierInvoicesActionTest(SettlementTestMixin, test.APITestCase):
    """POST project-credits/<uuid>/pay_earlier_invoices/, with and without
    dry_run, onto the current month or the month the cost was incurred."""

    def setUp(self):
        super().setUp()
        self.project_credit.pay_earlier_invoices = False
        self.project_credit.save()
        self.september = self.invoice(SEPTEMBER)
        self.september_item = self.item(self.september, 20000)
        self.october = self.invoice(OCTOBER, models.Invoice.States.PENDING)
        self.item(self.october, 10)
        self.url = factories.ProjectCreditFactory.get_url(
            self.project_credit, action="pay_earlier_invoices"
        )

    def post(self, payload, user="staff"):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(self.url, payload)

    def test_dry_run_says_what_would_be_written_and_writes_nothing(self):
        response = self.post({"dry_run": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        self.assertEqual(data["compensate_in"], "current_month")
        self.assertEqual(
            data["payments"],
            [
                {
                    "period": "2026-09-01",
                    "invoice_period": "2026-10-01",
                    "invoice_uuid": self.october.uuid.hex,
                    "resource_name": self.resource.name,
                    "resource_uuid": self.resource.uuid.hex,
                    "item_name": self.september_item.name,
                    "amount": "20000.00",
                }
            ],
        )
        self.assertEqual(data["project_credit_before"], "25000.00")
        self.assertEqual(data["project_credit_after"], "5000.00")
        self.assertEqual(data["customer_credit_after"], "80000.00")
        self.assertFalse(self.payments().exists())
        self.refresh()
        self.assertEqual(self.project_credit.value, 25000)
        self.assertFalse(self.project_credit.pay_earlier_invoices)

    def test_dry_run_onto_the_incurred_month_names_its_invoice(self):
        response = self.post({"dry_run": True, "compensate_in": "incurred_month"})

        payment = response.json()["payments"][0]
        self.assertEqual(payment["invoice_period"], "2026-09-01")
        self.assertEqual(payment["invoice_uuid"], self.september.uuid.hex)

    def test_paying_onto_the_current_month(self):
        response = self.post({})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.payments().get().invoice, self.october)
        self.refresh()
        self.assertTrue(self.project_credit.pay_earlier_invoices)
        self.assertEqual(self.project_credit.value, 5000)

    def test_paying_onto_the_incurred_month(self):
        response = self.post({"compensate_in": "incurred_month"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payment = self.payments().get()
        self.assertEqual(payment.invoice, self.september)
        self.assertEqual(payment.compensated_period, SEPTEMBER)
        self.refresh()
        self.assertEqual(
            self.project_credit.earlier_invoices_compensate_in, "incurred_month"
        )
        row = models.CreditTransaction.objects.get(
            project_credit=self.project_credit,
            transaction_type=models.CreditTransaction.Types.HISTORICAL_COMPENSATION,
        )
        self.assertEqual(row.billing_period, SEPTEMBER)
        self.assertEqual(row.reference, self.september)
        # Paid: nothing is left of September, and a second payment pays nothing.
        self.assertEqual(settlement.earlier_months(self.project_credit)[0].uncovered, 0)
        self.post({})
        self.assertEqual(self.paid(SEPTEMBER), 20000)

    def test_month_end_pays_onto_the_incurred_month(self):
        self.project_credit.pay_earlier_invoices = True
        self.project_credit.earlier_invoices_compensate_in = "incurred_month"
        self.project_credit.save()

        tasks.process_invoice_credits(self.october)

        self.assertEqual(self.payments().get().invoice, self.september)

    def test_owner_cannot_pay_earlier_invoices(self):
        response = self.post({"dry_run": True}, user="owner")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@freeze_time("2026-10-15")
class PayEarlierInvoicesPeriodTest(SettlementTestMixin, test.APITestCase):
    """The action previews and stores a period that is not saved yet."""

    def setUp(self):
        super().setUp()
        self.item(self.invoice(AUGUST), 1000)
        self.item(self.invoice(SEPTEMBER), 2000)
        self.invoice(OCTOBER, models.Invoice.States.PENDING)
        self.url = factories.ProjectCreditFactory.get_url(
            self.project_credit, action="pay_earlier_invoices"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_dry_run_previews_an_unsaved_period(self):
        response = self.client.post(
            self.url,
            {"dry_run": True, "earlier_invoices_from": "2026-09-01"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [payment["period"] for payment in response.json()["payments"]],
            ["2026-09-01"],
        )
        self.project_credit.refresh_from_db()
        self.assertIsNone(self.project_credit.earlier_invoices_from)

    def test_paying_stores_the_period(self):
        response = self.client.post(self.url, {"earlier_invoices_until": "2026-08-01"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.paid(AUGUST), 1000)
        self.assertEqual(self.paid(SEPTEMBER), 0)
        self.project_credit.refresh_from_db()
        self.assertEqual(self.project_credit.earlier_invoices_until, AUGUST)

    def test_a_period_not_on_the_first_is_refused(self):
        response = self.client.post(self.url, {"earlier_invoices_until": "2026-08-15"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
