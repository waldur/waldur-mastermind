"""The policy API says why a cost policy fires or not (`trigger_status`).

A list showing "Cost threshold 100, estimated cost 7 546, Fired: No" reads as a
broken policy when it is waiting for credit to run down. `trigger_status`
serves the gate and figures of the policy's own evaluation, so clients word the
reason from them instead of re-deriving it.
"""

import datetime
import decimal
from unittest import mock

from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import compensations as invoices_compensations
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import models
from waldur_mastermind.policy.structures import PolicyGates
from waldur_mastermind.policy.tests import factories as policy_factories

LIMIT = 100
TOTAL = models.ProjectEstimatedCostPolicy.Periods.TOTAL


class TriggerStatusTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        month_start = core_utils.month_start(datetime.date.today())
        self.invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )
        self.client.force_authenticate(self.fixture.staff)

    def _charge(self, amount):
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=self.resource,
            unit_price=amount,
            quantity=1,
        )

    def _earlier_uncovered_cost(self, amount="7701.67"):
        previous = core_utils.month_start(datetime.date.today()) - datetime.timedelta(
            days=1
        )
        old_invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=previous.year,
            month=previous.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=old_invoice,
            project=self.project,
            resource=self.resource,
            unit_price=decimal.Decimal(amount),
            quantity=1,
        )

    def _project_policy(self, **kwargs):
        defaults = dict(
            scope=self.project,
            limit_cost=LIMIT,
            actions="notify_project_team",
            period=TOTAL,
            use_credit=True,
        )
        defaults.update(kwargs)
        return policy_factories.ProjectEstimatedCostPolicyFactory(**defaults)

    def _status(self, factory, policy, **params):
        response = self.client.get(factory.get_url(policy), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data.get("trigger_status")

    def _project_status(self, policy):
        trigger_status = self._status(
            policy_factories.ProjectEstimatedCostPolicyFactory, policy
        )
        # It is the policy's own evaluation, so it cannot disagree with it.
        policy.refresh_from_db()
        self.assertEqual(trigger_status["triggered"], policy.is_triggered())
        return trigger_status

    def test_cost_below_limit(self):
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        self._charge(50)

        trigger_status = self._project_status(self._project_policy())

        self.assertEqual(trigger_status["gate"], PolicyGates.COST_BELOW_LIMIT)
        self.assertFalse(trigger_status["triggered"])
        self.assertIsNone(trigger_status["credit_value_compared"])

    def test_held_by_project_credit_reports_the_project_balance(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(50)

        trigger_status = self._project_status(self._project_policy())

        self.assertEqual(trigger_status["gate"], PolicyGates.HELD_BY_PROJECT_CREDIT)
        self.assertFalse(trigger_status["triggered"])
        self.assertEqual(trigger_status["credit_value_compared"], "2850.00")
        self.assertNotIn("message", trigger_status)

    def test_held_by_customer_credit(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)

        trigger_status = self._project_status(self._project_policy())

        self.assertEqual(trigger_status["gate"], PolicyGates.HELD_BY_CUSTOMER_CREDIT)
        self.assertEqual(trigger_status["credit_value_compared"], "2900.00")

    def test_credit_exhausted(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        self._charge(3000)

        trigger_status = self._project_status(self._project_policy())

        self.assertEqual(trigger_status["gate"], PolicyGates.CREDIT_EXHAUSTED)
        self.assertTrue(trigger_status["triggered"])
        self.assertEqual(trigger_status["credit_value_compared"], "0.00")

    def test_no_credit(self):
        self._earlier_uncovered_cost()

        trigger_status = self._project_status(self._project_policy())

        self.assertEqual(trigger_status["gate"], PolicyGates.NO_CREDIT)
        self.assertTrue(trigger_status["triggered"])

    def test_credit_ignored(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)

        trigger_status = self._project_status(self._project_policy(use_credit=False))

        self.assertEqual(trigger_status["gate"], PolicyGates.CREDIT_IGNORED)
        self.assertTrue(trigger_status["triggered"])

    def test_customer_and_offering_policies_serve_it_too(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)
        customer_policy = policy_factories.CustomerEstimatedCostPolicyFactory(
            scope=self.customer, limit_cost=LIMIT, period=TOTAL
        )
        offering_policy = policy_factories.OfferingEstimatedCostPolicyFactory(
            scope=self.resource.offering, limit_cost=LIMIT, period=TOTAL
        )

        customer_status = self._status(
            policy_factories.CustomerEstimatedCostPolicyFactory, customer_policy
        )
        offering_status = self._status(
            policy_factories.OfferingEstimatedCostPolicyFactory, offering_policy
        )

        self.assertEqual(customer_status["gate"], PolicyGates.HELD_BY_CUSTOMER_CREDIT)
        self.assertEqual(offering_status["gate"], PolicyGates.NO_CREDIT)

    def test_listing_a_customers_policies_shares_one_simulation(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        for limit in (100, 200, 300):
            self._project_policy(limit_cost=limit)

        with mock.patch.object(
            invoices_compensations.MonthlyCompensation,
            "__init__",
            autospec=True,
            side_effect=invoices_compensations.MonthlyCompensation.__init__,
        ) as init:
            response = self.client.get(
                policy_factories.ProjectEstimatedCostPolicyFactory.get_list_url()
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)
        self.assertTrue(all(row["trigger_status"] for row in response.data))
        # One for the customer, shared by current_cost, eta_days and
        # trigger_status of every row.
        self.assertEqual(init.call_count, 1)

    def test_left_out_by_field_selection_is_not_evaluated(self):
        policy = self._project_policy()

        with mock.patch.object(
            models.ProjectEstimatedCostPolicy, "evaluate"
        ) as evaluate:
            trigger_status = self._status(
                policy_factories.ProjectEstimatedCostPolicyFactory,
                policy,
                field=["uuid", "limit_cost"],
            )

        self.assertIsNone(trigger_status)
        evaluate.assert_not_called()

    def _count_cost_aggregations(self, **params):
        with mock.patch.object(
            models.ProjectEstimatedCostPolicy,
            "_cost_inputs",
            autospec=True,
            side_effect=models.ProjectEstimatedCostPolicy._cost_inputs,
        ) as cost_inputs:
            response = self.client.get(
                policy_factories.ProjectEstimatedCostPolicyFactory.get_list_url(),
                params,
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return cost_inputs.call_count, response.data

    def test_trigger_status_adds_no_invoice_aggregation(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)
        for limit in (100, 200, 300):
            self._project_policy(limit_cost=limit)

        without, _ = self._count_cost_aggregations(
            field=["uuid", "current_cost", "eta_days"]
        )
        with_status, rows = self._count_cost_aggregations()

        # current_cost reads the evaluation trigger_status runs anyway.
        self.assertEqual(with_status, without)
        for row in rows:
            policy = models.ProjectEstimatedCostPolicy.objects.get(uuid=row["uuid"])
            self.assertEqual(
                decimal.Decimal(row["current_cost"]),
                policy.get_current_cost().quantize(decimal.Decimal("0.01")),
            )

    def test_current_cost_alone_is_not_evaluated(self):
        self._earlier_uncovered_cost()
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=100000)
        policy = self._project_policy()

        with mock.patch.object(
            models.ProjectEstimatedCostPolicy,
            "evaluate",
            autospec=True,
            side_effect=models.ProjectEstimatedCostPolicy.evaluate,
        ) as evaluate:
            response = self.client.get(
                policy_factories.ProjectEstimatedCostPolicyFactory.get_url(policy),
                {"field": ["uuid", "current_cost"]},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        evaluate.assert_not_called()
        self.assertEqual(
            decimal.Decimal(response.data["current_cost"]),
            policy.get_current_cost().quantize(decimal.Decimal("0.01")),
        )


@ddt
class TriggerStatusVisibilityTest(test.APITestCase):
    """Project roles can list project policies, but the organization's credit
    describes the whole organization: it stays with owners and with roles that
    may view the organization's accounting, as on ProjectCredit."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        month_start = core_utils.month_start(datetime.date.today())
        invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=0,
        )
        previous = month_start - datetime.timedelta(days=1)
        invoice = invoices_factories.InvoiceFactory(
            customer=self.customer,
            year=previous.year,
            month=previous.month,
            tax_percent=0,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            resource=self.fixture.resource,
            unit_price=decimal.Decimal("7701.67"),
            quantity=1,
        )
        invoices_factories.CustomerCreditFactory(customer=self.customer, value=2900)
        self.policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            limit_cost=LIMIT,
            actions="notify_project_team",
            period=TOTAL,
            use_credit=True,
        )

    def _get(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            policy_factories.ProjectEstimatedCostPolicyFactory.get_url(self.policy)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    @data("admin", "manager", "member")
    def test_project_roles_do_not_see_the_organization_credit(self, role):
        row = self._get(getattr(self.fixture, role))

        self.assertNotIn("customer_credit", row)
        # They still see why the policy waits, just not the balance.
        self.assertEqual(
            row["trigger_status"]["gate"], PolicyGates.HELD_BY_CUSTOMER_CREDIT
        )
        self.assertIsNone(row["trigger_status"]["credit_value_compared"])

    @data("staff", "global_support", "owner")
    def test_customer_roles_see_the_organization_credit(self, role):
        row = self._get(getattr(self.fixture, role))

        self.assertEqual(row["customer_credit"], decimal.Decimal("2900.00"))
        self.assertEqual(row["trigger_status"]["credit_value_compared"], "2900.00")

    @data(True, False)
    def test_organization_credit_follows_view_accounting(self, granted):
        accountant = Role.objects.create(
            name="CUSTOMER.ACCOUNTANT",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        if granted:
            accountant.add_permission(PermissionEnum.VIEW_CUSTOMER_ACCOUNTING)
        user = structure_factories.UserFactory()
        self.customer.add_user(user, accountant)
        # A customer role without project access sees the policy through the
        # organization, as owners do.
        row = self._get(user)

        self.assertEqual("customer_credit" in row, granted)
        self.assertEqual(
            row["trigger_status"]["credit_value_compared"],
            "2900.00" if granted else None,
        )

    def test_project_roles_still_see_their_own_project_credit(self):
        invoices_factories.CustomerCreditFactory._meta.model.objects.filter(
            customer=self.customer
        ).update(value=100000)
        invoices_factories.ProjectCreditFactory(project=self.project, value=2900)

        row = self._get(self.fixture.member)

        self.assertEqual(
            row["trigger_status"]["gate"], PolicyGates.HELD_BY_PROJECT_CREDIT
        )
        self.assertEqual(row["trigger_status"]["credit_value_compared"], "2900.00")
