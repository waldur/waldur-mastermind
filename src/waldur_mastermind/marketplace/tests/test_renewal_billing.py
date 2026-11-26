"""Renewing a prepaid resource bills it as quoted: a raised limit for the rest of
the current term, and the extension at the new limits."""

import datetime
from decimal import Decimal

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import callbacks, models, utils
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderStates,
    OrderTypes,
    RenewalTerms,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories


@freeze_time("2024-01-15")
class PrepaidRenewalBillingTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(is_staff=True)
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING, state=OfferingStates.ACTIVE
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="storage",
            name="Storage",
        )
        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.component, price=Decimal("4.5")
        )
        self.project = self.offering.customer.projects.create(name="test-project")
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            limits={"storage": 50},
            end_date=datetime.date(2024, 4, 15),
        )
        self.resource.set_state_ok()
        self.resource.save()

    def _renewal_order(
        self, months, limits=None, state=OrderStates.EXECUTING, with_lines=True
    ):
        new_end_date = datetime.date(2024, 4, 15) + datetime.timedelta(days=31 * months)
        limits = limits or self.resource.limits
        # As the renew action records them; orders placed before it did have none.
        lines = (
            {
                "cost_lines": utils.serialize_renewal_lines(
                    self.resource.get_renewal_lines(months, limits)
                )
            }
            if with_lines
            else {}
        )
        return factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=state,
            type=OrderTypes.UPDATE,
            limits=limits,
            attributes={
                "action": "renew",
                "extension_months": months,
                "old_limits": self.resource.limits,
                "old_end_date": "2024-04-15",
                "new_end_date": new_end_date.isoformat(),
                "unit_prices": {"storage": "4.5"},
                **lines,
            },
            cost=self.resource.get_renewal_cost(months, limits),
        )

    def _invoiced_total(self):
        return sum(
            item.total
            for item in invoices_models.InvoiceItem.objects.filter(
                resource=self.resource
            )
        )

    def _renewal_items(self, order):
        return invoices_models.InvoiceItem.objects.filter(
            resource=self.resource, details__renewal_order_uuid=order.uuid.hex
        )

    def _supplementary_items(self):
        return [
            item
            for item in invoices_models.InvoiceItem.objects.filter(
                resource=self.resource
            )
            if item.details.get("is_supplementary")
        ]

    def test_renewal_is_invoiced_at_its_quote(self):
        order = self._renewal_order(12)

        utils.process_order(order, self.user)

        items = self._renewal_items(order)
        self.assertEqual(items.count(), 1)
        # 50 x 4.5 x 12
        self.assertEqual(sum(item.total for item in items), Decimal("2700"))
        self.assertEqual(sum(item.total for item in items), order.cost)
        item = items.get()
        self.assertEqual(item.invoice.month, 1)
        self.assertEqual(item.details["extension_months"], 12)
        self.assertEqual(item.details["old_end_date"], "2024-04-15")

    def test_renewal_with_higher_limits_charges_the_rest_of_the_current_term(self):
        before = self._invoiced_total()
        order = self._renewal_order(12, limits={"storage": 60})

        utils.process_order(order, self.user)

        items = self._renewal_items(order)
        current = items.get(details__renewal_term=RenewalTerms.CURRENT)
        extension = items.get(details__renewal_term=RenewalTerms.EXTENSION)
        # The 10 more for the 91 days left of the term, by the day:
        # 10 x 4.5 x 12 x 91 / 365 = 134.63..., rounded up to the cent.
        self.assertEqual(current.total, Decimal("134.64"))
        self.assertEqual(current.details["term_end"], "2024-04-15")
        # The extension at the new limits: 60 x 4.5 x 12.
        self.assertEqual(extension.total, Decimal("3240"))
        self.assertEqual(order.cost, Decimal("3374.64"))
        self.assertEqual(sum(item.total for item in items), order.cost)
        # Nothing else: the limit-change path does not bill the raise again.
        self.assertEqual(self._invoiced_total() - before, order.cost)
        self.assertEqual(self._supplementary_items(), [])

    def test_renewal_with_lower_limits_gives_no_credit_for_the_current_term(self):
        before = self._invoiced_total()
        order = self._renewal_order(12, limits={"storage": 40})

        utils.process_order(order, self.user)

        # The extension at the new limits only: 40 x 4.5 x 12. The current term
        # is prepaid, so the units dropped from it are not credited.
        self.assertEqual(order.cost, Decimal("2160"))
        self.assertEqual(self._renewal_items(order).count(), 1)
        self.assertEqual(self._invoiced_total() - before, order.cost)
        self.assertEqual(self._supplementary_items(), [])

    def test_asynchronous_renewal_with_higher_limits_is_invoiced_at_its_quote(self):
        before = self._invoiced_total()
        order = self._renewal_order(12, limits={"storage": 60})
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        callbacks.resource_update_succeeded(self.resource)

        self.assertEqual(order.cost, Decimal("3374.64"))
        self.assertEqual(self._invoiced_total() - before, order.cost)
        self.assertEqual(self._supplementary_items(), [])

    def test_renewal_after_the_end_date_has_no_current_term_to_charge(self):
        self.resource.end_date = datetime.date(2024, 1, 10)
        self.resource.save()
        order = self._renewal_order(12, limits={"storage": 60})

        utils.process_order(order, self.user)

        # Extended from today at the new limits, with no term left to top up.
        self.assertEqual(order.cost, Decimal("3240"))
        self.assertEqual(self._renewal_items(order).count(), 1)

    def test_renewal_order_placed_before_lines_were_recorded_is_billed_as_quoted(
        self,
    ):
        # Such an order was quoted at the extension alone.
        order = self._renewal_order(12, limits={"storage": 60}, with_lines=False)
        order.cost = Decimal("3240")
        order.save()
        before = self._invoiced_total()

        utils.process_order(order, self.user)

        self.assertEqual(self._invoiced_total() - before, Decimal("3240"))
        self.assertEqual(self._supplementary_items(), [])

    def test_renewal_is_invoiced_at_the_price_it_was_quoted_at(self):
        order = self._renewal_order(12)
        plan_component = self.plan.components.get()
        plan_component.price = Decimal("5.5")
        plan_component.save()

        utils.process_order(order, self.user)

        self.assertEqual(order.cost, Decimal("2700"))
        self.assertEqual(
            sum(item.total for item in self._renewal_items(order)), order.cost
        )
        self.assertEqual(self._renewal_items(order).get().unit_price, Decimal("4.5"))

    def test_renewal_order_quoted_with_no_lines_bills_nothing(self):
        order = self._renewal_order(12)
        order.attributes["cost_lines"] = []
        order.save()

        utils.process_order(order, self.user)

        self.assertFalse(self._renewal_items(order).exists())

    def test_renewal_order_without_recorded_prices_uses_the_plan_price(self):
        order = self._renewal_order(12, with_lines=False)
        del order.attributes["unit_prices"]
        order.save()

        utils.process_order(order, self.user)

        self.assertEqual(
            sum(item.total for item in self._renewal_items(order)), Decimal("2700")
        )

    def test_asynchronous_renewal_is_invoiced_once(self):
        order = self._renewal_order(12)
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        callbacks.resource_update_succeeded(self.resource)
        self.resource.refresh_from_db()
        order.refresh_from_db()
        MarketplaceBillingService.handle_renewal(self.resource, order)

        self.assertEqual(self._renewal_items(order).count(), 1)
        self.assertEqual(
            sum(item.total for item in self._renewal_items(order)), Decimal("2700")
        )

    def test_renewal_of_non_billable_offering_is_not_invoiced(self):
        self.offering.billable = False
        self.offering.save()
        order = self._renewal_order(12)

        utils.process_order(order, self.user)

        self.assertFalse(self._renewal_items(order).exists())

    def test_limit_change_after_a_renewal_is_still_billed(self):
        # set_limits changes limits without an order; the renewal stays the
        # latest update order but did not cause this change.
        utils.process_order(self._renewal_order(12), self.user)
        self.resource.refresh_from_db()

        self.resource.limits = {"storage": 60}
        self.resource.save()

        supplementary = self._supplementary_items()
        self.assertEqual(len(supplementary), 1)
        # 10 more until the renewed end date, 2025-04-22: 16 months started.
        self.assertEqual(supplementary[0].quantity, 160)

    def test_update_order_moving_end_date_without_renewal_bills_as_before(self):
        order = factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            limits={"storage": 60},
            attributes={
                "old_limits": self.resource.limits,
                "new_end_date": "2024-07-15",
            },
        )
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        callbacks.resource_update_succeeded(self.resource)

        self.assertFalse(self._renewal_items(order).exists())
        supplementary = self._supplementary_items()
        self.assertEqual(len(supplementary), 1)
        # 10 more until the new end date, as on develop: 6 months.
        self.assertEqual(supplementary[0].quantity, 60)

    def test_renewal_with_resource_projects_is_invoiced_the_same(self):
        self.offering.plugin_options = {
            "enable_resource_projects": True,
            "resource_projects_limit_policy": "aggregate",
        }
        self.offering.save()
        models.ResourceProject.objects.create(
            resource=self.resource, name="a", limits={"storage": 30}
        )
        models.ResourceProject.objects.create(
            resource=self.resource, name="b", limits={"storage": 20}
        )
        order = self._renewal_order(12)

        utils.process_order(order, self.user)

        # Billed on the resource's limits; its projects only share them out.
        self.assertEqual(
            sum(item.total for item in self._renewal_items(order)), Decimal("2700")
        )
        self.assertEqual(self._supplementary_items(), [])

    def test_renewal_counts_in_provider_revenue(self):
        provider = factories.ServiceProviderFactory(customer=self.offering.customer)
        utils.process_order(self._renewal_order(12), self.user)

        self.client.force_authenticate(self.user)
        response = self.client.get(
            factories.ServiceProviderFactory.get_url(provider, "revenue")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 675 for the original 3 months, 2700 for the renewal
        self.assertEqual(response.json()[0]["total"], 3375.0)

    def test_limit_update_outside_renewal_is_billed_as_before(self):
        self.resource.limits = {"storage": 60}
        self.resource.save()

        supplementary = self._supplementary_items()
        self.assertEqual(len(supplementary), 1)
        # 10 more for the 3 months left of the term
        self.assertEqual(supplementary[0].quantity, 30)
        self.assertFalse(
            invoices_models.InvoiceItem.objects.filter(
                resource=self.resource, details__has_key="renewal_order_uuid"
            ).exists()
        )

    def test_limit_raised_from_zero_is_billed_like_any_other_raise(self):
        self.resource.limits = {"storage": 0}
        self.resource.save()
        invoices_models.InvoiceItem.objects.filter(resource=self.resource).delete()

        self.resource.limits = {"storage": 10}
        self.resource.save()

        supplementary = self._supplementary_items()
        self.assertEqual(len(supplementary), 1)
        # 10 more for the 3 months left of the term, as from 50 to 60
        self.assertEqual(supplementary[0].quantity, 30)
        self.assertEqual(supplementary[0].unit_price, Decimal("4.5"))


@freeze_time("2026-11-25")
class RenewalQuoteTest(test.APITestCase):
    """15 TB at 2.56 per TB per month, paid until 25 Nov 2027, renewed on 25 Nov
    2026 for 12 months at 20 TB."""

    def setUp(self):
        self.user = structure_factories.UserFactory(is_staff=True)
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING, state=OfferingStates.ACTIVE
        )
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="storage",
            name="Storage",
            measured_unit="TB",
        )
        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=component, price=Decimal("2.56")
        )
        project = self.offering.customer.projects.create(name="test-project")
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=project,
            limits={"storage": 15},
            end_date=datetime.date(2027, 11, 25),
        )
        self.resource.set_state_ok()
        self.resource.save()

    def test_raise_is_charged_for_the_rest_of_the_term_and_the_extension(self):
        current, extension = self.resource.get_renewal_lines(12, {"storage": 20})

        # 5 TB x 2.56 x 12 x 365 / 365, from today to the current end date.
        self.assertEqual(current["term"], RenewalTerms.CURRENT)
        self.assertEqual(current["start"], datetime.date(2026, 11, 25))
        self.assertEqual(current["end"], datetime.date(2027, 11, 25))
        self.assertEqual(current["total"], Decimal("153.60"))
        # 20 TB x 2.56 x 12, from the current end date to the new one.
        self.assertEqual(extension["term"], RenewalTerms.EXTENSION)
        self.assertEqual(extension["start"], datetime.date(2027, 11, 25))
        self.assertEqual(extension["end"], datetime.date(2028, 11, 25))
        self.assertEqual(extension["total"], Decimal("614.40"))
        self.assertEqual(
            self.resource.get_renewal_cost(12, {"storage": 20}), Decimal("768.00")
        )

    def test_estimate_shows_each_term_on_its_own_line(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "estimate_renewal"),
            {"extension_months": 12, "limits": {"storage": 20}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        lines = {line["term"]: line for line in response.json()["components"]}
        self.assertEqual(Decimal(lines["current_term"]["total"]), Decimal("153.60"))
        self.assertEqual(lines["current_term"]["period_end"], "2027-11-25")
        self.assertEqual(Decimal(lines["current_term"]["quantity"]), Decimal("60"))
        self.assertEqual(Decimal(lines["extension"]["total"]), Decimal("614.40"))
        self.assertEqual(Decimal(lines["extension"]["quantity"]), Decimal("240"))
        self.assertEqual(Decimal(response.data["total"]), Decimal("768.00"))

    def test_estimate_without_a_raise_has_no_current_term_line(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "estimate_renewal"),
            {"extension_months": 12, "limits": {"storage": 10}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [line["term"] for line in response.data["components"]], ["extension"]
        )
        # 10 TB x 2.56 x 12, no credit for the 5 TB dropped from the paid term.
        self.assertEqual(Decimal(response.data["total"]), Decimal("307.20"))

    def _lower_from_20_tb(self):
        self.resource.limits = {"storage": 20}
        self.resource.save()
        self.offering.plugin_options = {
            "renewal_limit_decrease_policy": "stretch_paid_term"
        }
        self.offering.save()

    def test_lower_limit_is_not_stretched_by_default(self):
        self.resource.limits = {"storage": 20}
        self.resource.save()

        quote = self.resource.get_renewal_quote(12, {"storage": 15})

        # no_credit: the extension starts at the current end date, at 15 TB.
        self.assertEqual(quote["paid_up_end_date"], datetime.date(2027, 11, 25))
        self.assertEqual(quote["new_end_date"], datetime.date(2028, 11, 25))
        self.assertEqual(
            self.resource.get_renewal_cost(12, {"storage": 15}), Decimal("460.80")
        )

    def _add_cores(self):
        """A second prepaid component: 4 cores at 10 per month, with 20 TB, on an
        offering that stretches the paid-up term."""
        self._lower_from_20_tb()
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="cores",
            name="Cores",
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=component, price=Decimal("10")
        )
        self.resource.limits = {"storage": 20, "cores": 4}
        self.resource.save()

    def test_lower_limit_spreads_the_paid_term_over_it(self):
        self._lower_from_20_tb()

        quote = self.resource.get_renewal_quote(12, {"storage": 15})

        # 20 TB x 365 days already paid lasts 365 x 20 / 15 = 486.7 -> 487 days
        # at 15 TB; the extension starts there.
        self.assertEqual(quote["paid_up_end_date"], datetime.date(2028, 3, 26))
        self.assertEqual(quote["new_end_date"], datetime.date(2029, 3, 26))
        (extension,) = quote["lines"]
        self.assertEqual(extension["term"], RenewalTerms.EXTENSION)
        self.assertEqual(extension["start"], datetime.date(2028, 3, 26))
        # Charged the extension only, at the new limit: 15 x 2.56 x 12.
        self.assertEqual(extension["total"], Decimal("460.80"))

    def test_lower_limit_renewal_extends_to_the_stretched_end_and_gives_no_credit(
        self,
    ):
        self._lower_from_20_tb()
        before = invoices_models.InvoiceItem.objects.filter(
            resource=self.resource
        ).count()
        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "renew"),
            {"extension_months": 12, "limits": {"storage": 15}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(resource=self.resource, type=OrderTypes.UPDATE)
        self.assertEqual(order.attributes["paid_up_end_date"], "2028-03-26")
        self.assertEqual(order.attributes["new_end_date"], "2029-03-26")
        self.assertEqual(order.cost, Decimal("460.80"))
        order.set_state_executing()
        order.save()

        utils.process_order(order, self.user)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, {"storage": 15})
        self.assertEqual(self.resource.end_date, datetime.date(2029, 3, 26))
        items = invoices_models.InvoiceItem.objects.filter(resource=self.resource)
        self.assertEqual(items.count(), before + 1)
        self.assertFalse(any(item.unit_price < 0 for item in items))
        self.assertEqual(
            sum(
                item.total
                for item in items.filter(details__renewal_order_uuid=order.uuid.hex)
            ),
            Decimal("460.80"),
        )

    def test_estimate_shows_the_stretched_paid_up_end(self):
        self._lower_from_20_tb()
        self.client.force_authenticate(self.user)

        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "estimate_renewal"),
            {"extension_months": 12, "limits": {"storage": 15}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        self.assertEqual(data["paid_up_end_date"], "2028-03-26")
        self.assertEqual(data["new_end_date"], "2029-03-26")
        self.assertEqual(data["components"][0]["period_start"], "2028-03-26")
        self.assertEqual(Decimal(data["total"]), Decimal("460.80"))

    def test_raise_is_not_stretched(self):
        quote = self.resource.get_renewal_quote(12, {"storage": 20})

        self.assertEqual(quote["paid_up_end_date"], datetime.date(2027, 11, 25))
        self.assertEqual(quote["new_end_date"], datetime.date(2028, 11, 25))

    def test_unchanged_limits_are_not_stretched(self):
        quote = self.resource.get_renewal_quote(12)

        self.assertEqual(quote["paid_up_end_date"], datetime.date(2027, 11, 25))

    def test_lower_limit_after_the_end_date_is_not_stretched(self):
        self._lower_from_20_tb()
        self.resource.end_date = datetime.date(2026, 11, 1)
        self.resource.save()

        quote = self.resource.get_renewal_quote(12, {"storage": 15})

        self.assertEqual(quote["paid_up_end_date"], datetime.date(2026, 11, 25))
        self.assertEqual(quote["new_end_date"], datetime.date(2027, 11, 25))

    def test_stretch_spans_components_by_value(self):
        self._add_cores()

        # Paid per month: 20 x 2.56 + 4 x 10 = 91.20; new: 15 x 2.56 + 4 x 10 =
        # 78.40. 365 x 91.20 / 78.40 = 424.6 -> 425 days.
        quote = self.resource.get_renewal_quote(12, {"storage": 15, "cores": 4})

        self.assertEqual(
            quote["paid_up_end_date"],
            datetime.date(2026, 11, 25) + datetime.timedelta(days=425),
        )

    def test_component_left_out_of_the_new_limits_keeps_its_limit(self):
        self._add_cores()

        # Only storage is sent; cores keep 4 rather than reading as 0, which
        # would stretch the paid-up period by the cores' whole value.
        quote = self.resource.get_renewal_quote(12, {"storage": 20})

        self.assertEqual(quote["paid_up_end_date"], datetime.date(2027, 11, 25))
        cores = next(
            line for line in quote["lines"] if line["component_type"] == "cores"
        )
        self.assertEqual(cores["new_limit"], Decimal(4))

    def test_renew_with_partial_limits_orders_the_full_limits(self):
        self._add_cores()
        self.client.force_authenticate(self.user)

        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "renew"),
            {"extension_months": 12, "limits": {"storage": 15}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(resource=self.resource, type=OrderTypes.UPDATE)
        self.assertEqual(order.limits, {"storage": 15, "cores": 4})

    def test_raise_and_lower_in_one_order_is_not_stretched(self):
        self._add_cores()

        quote = self.resource.get_renewal_quote(12, {"storage": 15, "cores": 8})

        # The raise is charged for the rest of the term; nothing is stretched.
        self.assertEqual(quote["paid_up_end_date"], datetime.date(2027, 11, 25))
        self.assertEqual(
            sorted((line["component_type"], line["term"]) for line in quote["lines"]),
            [
                ("cores", RenewalTerms.CURRENT),
                ("cores", RenewalTerms.EXTENSION),
                ("storage", RenewalTerms.EXTENSION),
            ],
        )


@freeze_time("2024-01-15")
class RenewalOrderTermsTest(test.APITestCase):
    """A pending renewal's terms stay as quoted when the order is edited or approved.

    Everything goes through the API with the roles permissions.yaml grants.
    """

    def setUp(self):
        for permission in (
            PermissionEnum.APPROVE_ORDER,
            PermissionEnum.CREATE_ORDER,
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
        ):
            CustomerRole.OWNER.add_permission(permission)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING, state=OfferingStates.ACTIVE
        )
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="storage",
            name="Storage",
        )
        self.plan = factories.PlanFactory(
            offering=self.offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=component, price=Decimal("4.5")
        )
        consumer = structure_fixtures.ProjectFixture()
        self.project_admin = consumer.admin
        self.consumer_owner = consumer.owner
        self.provider_owner = structure_factories.UserFactory()
        self.offering.customer.add_user(self.provider_owner, CustomerRole.OWNER)
        factories.ServiceProviderFactory(customer=self.offering.customer)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=consumer.project,
            limits={"storage": 50},
            end_date=datetime.date(2024, 4, 15),
        )
        self.resource.set_state_ok()
        self.resource.save()

    def _renew(self):
        self.client.force_authenticate(self.project_admin)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "renew"),
            {"extension_months": 12},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return models.Order.objects.get(resource=self.resource, type=OrderTypes.UPDATE)

    def _edit_attributes(self, order, **changes):
        order.refresh_from_db()
        self.client.force_authenticate(self.consumer_owner)
        response = self.client.patch(
            factories.OrderFactory.get_url(order),
            {"attributes": dict(order.attributes, **changes)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()

    def _approve_and_complete(self, order, provider_attributes=None):
        self.client.force_authenticate(self.consumer_owner)
        response = self.client.post(
            factories.OrderFactory.get_url(order, "approve_by_consumer")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.client.force_authenticate(self.provider_owner)
        payload = {"attributes": provider_attributes} if provider_attributes else {}
        response = self.client.post(
            factories.OrderFactory.get_url(order, "approve_by_provider"),
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        utils.process_order(order, self.staff)
        order.refresh_from_db()
        self.resource.refresh_from_db()

    def _invoiced_total(self):
        return sum(
            item.total
            for item in invoices_models.InvoiceItem.objects.filter(
                resource=self.resource
            )
        )

    def _assert_renewed_as_quoted(self, order, invoiced_before):
        self.assertEqual(order.state, OrderStates.DONE)
        # 50 x 4.5 x 12
        self.assertEqual(order.cost, Decimal("2700"))
        self.assertEqual(self._invoiced_total() - invoiced_before, order.cost)
        self.assertEqual(self.resource.end_date, datetime.date(2025, 4, 15))

    def test_renewal_is_invoiced_at_its_quote(self):
        order = self._renew()
        before = self._invoiced_total()

        self._approve_and_complete(order)

        self._assert_renewed_as_quoted(order, before)

    def test_renewal_with_higher_limits_is_invoiced_at_its_recorded_lines(self):
        self.client.force_authenticate(self.project_admin)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "renew"),
            {"extension_months": 12, "limits": {"storage": 60}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(resource=self.resource, type=OrderTypes.UPDATE)
        before = self._invoiced_total()

        self._approve_and_complete(order)

        self.assertEqual(
            [line["term"] for line in order.attributes["cost_lines"]],
            [RenewalTerms.CURRENT, RenewalTerms.EXTENSION],
        )
        # 134.64 for the 91 days left of the term, 3240 for the extension.
        self.assertEqual(order.cost, Decimal("3374.64"))
        self.assertEqual(self._invoiced_total() - before, order.cost)

    def test_consumer_edit_cannot_change_the_quoted_lines(self):
        order = self._renew()
        quoted = order.attributes["cost_lines"]

        self._edit_attributes(
            order, cost_lines=[dict(quoted[0], unit_price="0", total="0")]
        )

        self.assertEqual(order.attributes["cost_lines"], quoted)

    def test_consumer_edit_keeps_the_quoted_price(self):
        order = self._renew()
        quoted = order.attributes["unit_prices"]

        self._edit_attributes(order, unit_prices={"storage": "0"})

        self.assertEqual(order.attributes["unit_prices"], quoted)
        before = self._invoiced_total()
        self._approve_and_complete(order)
        self._assert_renewed_as_quoted(order, before)

    def test_consumer_edit_with_a_malformed_price_does_not_fail_the_renewal(self):
        order = self._renew()

        self._edit_attributes(order, unit_prices={"storage": "abc"})

        before = self._invoiced_total()
        self._approve_and_complete(order)
        self._assert_renewed_as_quoted(order, before)

    def test_consumer_edit_keeps_the_extension_months(self):
        order = self._renew()

        self._edit_attributes(order, extension_months=1)

        self.assertEqual(order.attributes["extension_months"], 12)
        before = self._invoiced_total()
        self._approve_and_complete(order)
        self._assert_renewed_as_quoted(order, before)

    def test_consumer_edit_keeps_the_new_end_date(self):
        order = self._renew()

        self._edit_attributes(order, new_end_date="2030-04-15")

        before = self._invoiced_total()
        self._approve_and_complete(order)
        self._assert_renewed_as_quoted(order, before)

    def test_consumer_edit_cannot_drop_the_renewal_terms(self):
        order = self._renew()
        terms = dict(order.attributes)
        self.client.force_authenticate(self.consumer_owner)

        response = self.client.patch(
            factories.OrderFactory.get_url(order),
            {"attributes": {"name": terms["name"]}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.attributes, terms)

    def test_consumer_edit_cannot_make_a_limit_change_a_renewal(self):
        order = factories.OrderFactory(
            resource=self.resource,
            project=self.resource.project,
            offering=self.offering,
            plan=self.plan,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.UPDATE,
            limits={"storage": 60},
            attributes={"old_limits": self.resource.limits},
        )

        self._edit_attributes(
            order,
            action="renew",
            extension_months=60,
            new_end_date="2030-04-15",
            unit_prices={"storage": "0"},
        )

        self.assertEqual(order.attributes, {"old_limits": {"storage": 50}})

    def test_consumer_edit_still_changes_other_attributes(self):
        order = self._renew()

        self._edit_attributes(order, name="renamed")

        self.assertEqual(order.attributes["name"], "renamed")
        self.assertEqual(order.attributes["action"], "renew")

    def test_consumer_cannot_change_a_renewals_limits(self):
        order = self._renew()
        self.client.force_authenticate(self.consumer_owner)

        response = self.client.patch(
            factories.OrderFactory.get_url(order),
            {"limits": {"storage": 60}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.limits, {"storage": 50})
        before = self._invoiced_total()
        self._approve_and_complete(order)
        self._assert_renewed_as_quoted(order, before)

    def test_consumer_can_resend_a_renewals_own_limits(self):
        order = self._renew()
        self.client.force_authenticate(self.consumer_owner)

        response = self.client.patch(
            factories.OrderFactory.get_url(order),
            {"limits": {"storage": 50}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.cost, Decimal("2700"))

    def test_provider_approval_keeps_the_renewal_terms(self):
        order = self._renew()
        terms = dict(order.attributes)
        before = self._invoiced_total()

        self._approve_and_complete(
            order,
            provider_attributes={
                "unit_prices": {"storage": "100"},
                "extension_months": 60,
                "new_end_date": "2030-04-15",
            },
        )

        self.assertEqual(order.attributes, terms)
        self._assert_renewed_as_quoted(order, before)

    def test_provider_approval_cannot_make_a_limit_change_a_renewal(self):
        order = factories.OrderFactory(
            resource=self.resource,
            project=self.resource.project,
            offering=self.offering,
            plan=self.plan,
            state=OrderStates.PENDING_PROVIDER,
            type=OrderTypes.UPDATE,
            limits={"storage": 60},
            attributes={"old_limits": self.resource.limits},
        )
        self.client.force_authenticate(self.provider_owner)

        response = self.client.post(
            factories.OrderFactory.get_url(order, "approve_by_provider"),
            {
                "attributes": {
                    "action": "renew",
                    "extension_months": 60,
                    "new_end_date": "2030-04-15",
                }
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order.refresh_from_db()
        self.assertEqual(order.attributes, {"old_limits": {"storage": 50}})
