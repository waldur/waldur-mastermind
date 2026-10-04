"""Service provider managers report usage for their organization's offerings.

The role is held on the ServiceProvider record rather than on the customer, so
the usage endpoints reach it through customer.serviceprovider.
"""

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import OfferingRole, ServiceProviderRole
from waldur_core.permissions.tests.test_system_role_descriptions import (
    permissions_yaml_rows,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import BillingTypes, OrderStates, OrderTypes
from waldur_mastermind.marketplace.tests import factories

USAGES_URL = "/api/marketplace-component-usages/"
USER_USAGES_URL = "/api/marketplace-component-user-usages/"
SET_USAGE_URL = USAGES_URL + "set_usage/"


@freeze_time("2026-06-19")
class ServiceProviderManagerUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        plan = factories.PlanFactory(offering=offering)
        component = factories.OfferingComponentFactory(
            offering=offering, billing_type=BillingTypes.USAGE, type="cpu"
        )
        factories.PlanComponentFactory(plan=plan, component=component)
        self.resource = models.Resource.objects.create(
            offering=offering, plan=plan, project=self.fixture.project
        )
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=plan,
        )
        callbacks.resource_creation_succeeded(self.resource)
        self.plan_period = models.ResourcePlanPeriod.objects.get(resource=self.resource)
        # Tests run without permissions.yaml; this mirrors the shipped role.
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        OfferingRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_USAGE)
        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)

    def set_usage(self, user, **extra):
        self.client.force_authenticate(user)
        return self.client.post(
            SET_USAGE_URL,
            {
                "plan_period": self.plan_period.uuid.hex,
                "usages": [{"type": "cpu", "amount": 5}],
                **extra,
            },
            format="json",
        )

    def test_shipped_role_carries_set_usage(self):
        rows = permissions_yaml_rows()
        if rows is None:
            self.skipTest("permissions.yaml is not part of this checkout")
        manager = next(row for row in rows if row["role"] == "CUSTOMER.MANAGER")
        self.assertIn(PermissionEnum.SET_RESOURCE_USAGE.value, manager["permissions"])

    def test_manager_reports_usage(self):
        response = self.set_usage(self.manager)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages, {"cpu": 5.0})

    def test_manager_dates_usage_like_an_owner(self):
        response = self.set_usage(self.manager, date=timezone.now().isoformat())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_manager_of_another_provider_is_refused(self):
        other = factories.ServiceProviderFactory()
        outsider = structure_factories.UserFactory()
        other.add_user(outsider, ServiceProviderRole.MANAGER)

        response = self.set_usage(outsider)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def usage(self):
        self.set_usage(self.manager)
        return models.ComponentUsage.objects.get(resource=self.resource)

    def set_user_usage(self, user, usage):
        self.client.force_authenticate(user)
        return self.client.post(
            f"{USAGES_URL}{usage.uuid.hex}/set_user_usage/",
            {"usage": 2, "username": "user_00"},
            format="json",
        )

    def listed(self, user, url=USAGES_URL):
        self.client.force_authenticate(user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def test_manager_reports_user_usage_and_reads_it_back(self):
        usage = self.usage()

        response = self.set_user_usage(self.manager, usage)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn(usage.uuid.hex, self.listed(self.manager))
        user_usage = models.ComponentUserUsage.objects.get(component_usage=usage)
        self.assertIn(user_usage.uuid.hex, self.listed(self.manager, USER_USAGES_URL))

    def test_offering_manager_reports_user_usage(self):
        usage = self.usage()
        offering_manager = structure_factories.UserFactory()
        self.resource.offering.add_user(offering_manager, OfferingRole.MANAGER)

        response = self.set_user_usage(offering_manager, usage)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_other_providers_and_consumers_see_nothing_new(self):
        usage = self.usage()
        other_manager = structure_factories.UserFactory()
        factories.ServiceProviderFactory().add_user(
            other_manager, ServiceProviderRole.MANAGER
        )
        other_project_member = structure_fixtures.ProjectFixture().admin

        for user in (other_manager, other_project_member):
            self.assertNotIn(usage.uuid.hex, self.listed(user))
            self.assertEqual(
                self.set_user_usage(user, usage).status_code,
                status.HTTP_404_NOT_FOUND,
            )

    def test_a_manager_without_the_permission_sees_nothing(self):
        usage = self.usage()
        ServiceProviderRole.MANAGER.delete_permission(PermissionEnum.SET_RESOURCE_USAGE)

        self.assertNotIn(usage.uuid.hex, self.listed(self.manager))
