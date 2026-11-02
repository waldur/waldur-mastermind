"""Marketplace writes reserved to staff. A staff user's personal access token
without `STAFF.ACCESS` acts as the user without staff (#980), even where it
carries the permission the endpoint asks for.
"""

import datetime

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import utils as core_utils
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests.pat_utils import STAFF, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories

SET_USAGE = PermissionEnum.SET_RESOURCE_USAGE.value
UPDATE_INTEGRATION = PermissionEnum.UPDATE_OFFERING_INTEGRATION.value
# December 2018: a closed billing period with no invoice.
BACKFILL_DATE = datetime.datetime(2018, 12, 15, 10, 0, tzinfo=datetime.UTC)


@freeze_time("2019-06-19")
@override_config(PAT_ENABLED=True)
class UsageBackfillTokenScopeTest(test.APITestCase):
    """Backfilling usage of a usage-based component into a closed period is
    reserved to staff; a provider may only when the period's invoice is still
    open."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering, billing_type=BillingTypes.USAGE, type="cpu"
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )
        self.resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def set_usage(self):
        return self.client.post(
            "/api/marketplace-component-usages/set_usage/",
            {
                "plan_period": self.plan_period.uuid.hex,
                "date": BACKFILL_DATE.isoformat(),
                "usages": [{"type": "cpu", "amount": 10}],
            },
            format="json",
        )

    def set_user_usage(self):
        component_usage = models.ComponentUsage.objects.create(
            resource=self.resource,
            plan_period=self.plan_period,
            component=self.offering_component,
            usage=100,
            date=datetime.datetime.now(tz=datetime.UTC),
            billing_period=core_utils.month_start(
                datetime.datetime.now(tz=datetime.UTC)
            ),
        )
        return self.client.post(
            f"/api/marketplace-component-usages/{component_usage.uuid.hex}"
            "/set_user_usage/",
            {"username": "user123", "usage": 25, "date": BACKFILL_DATE.isoformat()},
            format="json",
        )

    def backfilled(self):
        return models.ComponentUsage.objects.filter(
            resource=self.resource, billing_period=datetime.date(2018, 12, 1)
        ).exists()

    def test_staff_session_can_backfill(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.set_usage().status_code, status.HTTP_201_CREATED)
        self.assertTrue(self.backfilled())

    def test_staff_token_with_staff_scope_can_backfill(self):
        use_pat(self.client, self.staff, STAFF, SET_USAGE)
        self.assertEqual(self.set_usage().status_code, status.HTTP_201_CREATED)
        self.assertTrue(self.backfilled())

    def test_staff_token_without_staff_scope_cannot_backfill(self):
        use_pat(self.client, self.staff, SET_USAGE)
        self.assertEqual(self.set_usage().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.backfilled())

    def test_provider_owner_cannot_backfill(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.set_usage().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.backfilled())

    def test_staff_token_with_staff_scope_can_backfill_user_usage(self):
        use_pat(self.client, self.staff, STAFF, SET_USAGE)
        self.assertEqual(self.set_user_usage().status_code, status.HTTP_201_CREATED)
        self.assertTrue(self.backfilled())

    def test_staff_token_without_staff_scope_cannot_backfill_user_usage(self):
        use_pat(self.client, self.staff, SET_USAGE)
        self.assertEqual(self.set_user_usage().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.backfilled())

    def test_provider_owner_cannot_backfill_user_usage(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.set_user_usage().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.backfilled())


@override_config(PAT_ENABLED=True)
class StaffOnlyPluginOptionsTokenScopeTest(test.APITestCase):
    """`update_integration` takes the staff-only plugin options from staff
    only. The token carries `OFFERING.UPDATE_INTEGRATION`, which the action
    itself needs."""

    OPTIONS = {
        "auto_approve_for_roles": [RoleEnum.PROJECT_MANAGER],
        "disable_grace_period": True,
        "disabled_resource_actions": ["terminate"],
    }

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        ProjectRole.MANAGER  # materialize the role so name validation passes
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def update(self, name):
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(
            url, {"plugin_options": {name: self.OPTIONS[name]}}, format="json"
        )

    def assert_updates(self, allowed):
        for name, value in self.OPTIONS.items():
            with self.subTest(option=name):
                response = self.update(name)
                self.offering.refresh_from_db()
                if allowed:
                    self.assertEqual(
                        response.status_code, status.HTTP_200_OK, response.data
                    )
                    self.assertEqual(self.offering.plugin_options[name], value)
                else:
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertNotIn(name, self.offering.plugin_options)

    def test_staff_session_can_set_staff_only_options(self):
        self.client.force_authenticate(self.staff)
        self.assert_updates(True)

    def test_staff_token_with_staff_scope_can_set_staff_only_options(self):
        use_pat(self.client, self.staff, STAFF, UPDATE_INTEGRATION)
        self.assert_updates(True)

    def test_staff_token_without_staff_scope_cannot_set_staff_only_options(self):
        use_pat(self.client, self.staff, UPDATE_INTEGRATION)
        self.assert_updates(False)

    def test_owner_cannot_set_staff_only_options(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assert_updates(False)


@override_config(PAT_ENABLED=True)
class ServiceProviderAllowedDomainsTokenScopeTest(test.APITestCase):
    """`allowed_domains` is writable by staff only. The staff user here also
    owns the provider's organization, so the update itself is allowed."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer, allowed_domains=["original.com"]
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.fixture.customer.add_user(self.staff, CustomerRole.OWNER)

    def set_domains(self):
        response = self.client.patch(
            factories.ServiceProviderFactory.get_url(self.service_provider),
            {"allowed_domains": ["example.com"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.service_provider.refresh_from_db()
        return self.service_provider.allowed_domains

    def test_staff_session_sets_allowed_domains(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.set_domains(), ["example.com"])

    def test_staff_token_with_staff_scope_sets_allowed_domains(self):
        use_pat(self.client, self.staff, STAFF)
        self.assertEqual(self.set_domains(), ["example.com"])

    def test_staff_token_without_staff_scope_cannot_set_allowed_domains(self):
        use_pat(self.client, self.staff)
        self.assertEqual(self.set_domains(), ["original.com"])

    def test_owner_cannot_set_allowed_domains(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.set_domains(), ["original.com"])
