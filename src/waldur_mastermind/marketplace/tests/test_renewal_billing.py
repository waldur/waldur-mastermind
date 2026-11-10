"""Renewing a prepaid resource bills the extension as quoted (#1019)."""

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

    def _renewal_order(self, months, limits=None, state=OrderStates.EXECUTING):
        new_end_date = datetime.date(2024, 4, 15) + datetime.timedelta(days=31 * months)
        limits = limits or self.resource.limits
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

    def test_renewal_with_higher_limits_is_invoiced_at_its_quote(self):
        before = self._invoiced_total()
        order = self._renewal_order(12, limits={"storage": 60})

        utils.process_order(order, self.user)

        # The extension at the new limits: 60 x 4.5 x 12, and nothing else. The
        # quote does not charge the raise for the rest of the paid term.
        self.assertEqual(order.cost, Decimal("3240"))
        self.assertEqual(
            sum(item.total for item in self._renewal_items(order)), order.cost
        )
        self.assertEqual(self._invoiced_total() - before, order.cost)
        self.assertEqual(self._supplementary_items(), [])

    def test_renewal_with_lower_limits_is_invoiced_at_its_quote(self):
        before = self._invoiced_total()
        order = self._renewal_order(12, limits={"storage": 40})

        utils.process_order(order, self.user)

        # The extension at the new limits: 40 x 4.5 x 12. The quote gives no
        # credit for the units dropped for the rest of the paid term.
        self.assertEqual(order.cost, Decimal("2160"))
        self.assertEqual(self._invoiced_total() - before, order.cost)
        self.assertEqual(self._supplementary_items(), [])

    def test_asynchronous_renewal_with_higher_limits_is_invoiced_at_its_quote(self):
        before = self._invoiced_total()
        order = self._renewal_order(12, limits={"storage": 60})
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        callbacks.resource_update_succeeded(self.resource)

        self.assertEqual(self._invoiced_total() - before, order.cost)
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

    def test_renewal_order_without_recorded_prices_uses_the_plan_price(self):
        order = self._renewal_order(12)
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
