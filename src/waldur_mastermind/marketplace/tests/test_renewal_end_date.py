"""A renewal counts from the deployment's local date, as the end-date sweeps do.

The sweeps that terminate resources take "today" from ``timezone.localdate()``.
A renewal that took it from the UTC date instead would, on a deployment ahead
of UTC, extend an expired resource from yesterday in the first hours after
local midnight.
"""

import datetime
from decimal import Decimal

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories

# 25 Nov 00:30 in Tallinn (UTC+2) is still 24 Nov in UTC.
JUST_AFTER_LOCAL_MIDNIGHT = "2026-11-24 22:30:00"


@freeze_time(JUST_AFTER_LOCAL_MIDNIGHT)
class RenewalEndDateTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        offering = factories.OfferingFactory(
            type=BASIC_OFFERING, state=OfferingStates.ACTIVE
        )
        component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
            type="storage",
            name="Storage",
        )
        plan = factories.PlanFactory(
            offering=offering,
            unit=models.Plan.Units.PER_MONTH,
            unit_price=Decimal("0"),
        )
        factories.PlanComponentFactory(
            plan=plan, component=component, price=Decimal("4.5")
        )
        consumer = structure_fixtures.ProjectFixture()
        self.project_admin = consumer.admin
        self.resource = factories.ResourceFactory(
            offering=offering,
            plan=plan,
            project=consumer.project,
            limits={"storage": 50},
            # Long expired, so the renewal counts from today.
            end_date=datetime.date(2026, 3, 8),
        )
        self.resource.set_state_ok()
        self.resource.save()

    def _estimated_end_date(self):
        self.client.force_authenticate(self.project_admin)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "estimate_renewal"),
            {"extension_months": 12},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["new_end_date"]

    def _ordered_end_date(self):
        self.client.force_authenticate(self.project_admin)
        response = self.client.post(
            factories.ResourceFactory.get_url(self.resource, "renew"),
            {"extension_months": 12},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(resource=self.resource, type=OrderTypes.UPDATE)
        return order.attributes["new_end_date"]

    @override_settings(TIME_ZONE="Europe/Tallinn")
    def test_estimate_counts_from_the_local_date(self):
        self.assertEqual(str(self._estimated_end_date()), "2027-11-25")

    @override_settings(TIME_ZONE="Europe/Tallinn")
    def test_renewal_order_counts_from_the_local_date(self):
        self.assertEqual(self._ordered_end_date(), "2027-11-25")

    @override_settings(TIME_ZONE="UTC")
    def test_utc_deployment_is_unchanged(self):
        self.assertEqual(str(self._estimated_end_date()), "2027-11-24")
        self.assertEqual(self._ordered_end_date(), "2027-11-24")
