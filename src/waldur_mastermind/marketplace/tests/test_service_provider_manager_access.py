"""A service provider manager holds their role on the ServiceProvider, not on its
customer. Provider-side reads must accept that scope, not only a customer role."""

from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.serializers import CustomerSerializer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.models import ServiceProvider
from waldur_mastermind.marketplace.tests import factories, fixtures


class ServiceProviderManagerAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.offering = self.fixture.offering

        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_REVENUE
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMER_PROJECTS
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_PROJECTS
        )
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_USERS
        )

        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)

        # Manages a different provider, so holds the same role but not here.
        self.other_manager = structure_factories.UserFactory()
        factories.ServiceProviderFactory().add_user(
            self.other_manager, ServiceProviderRole.MANAGER
        )

    def get_provider_offerings(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            factories.OfferingFactory.get_list_url(),
            {"customer_uuid": self.service_provider.customer.uuid.hex},
        )

    def test_manager_lists_own_provider_offerings(self):
        response = self.get_provider_offerings(self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["uuid"] for item in response.data], [self.offering.uuid.hex]
        )

    def test_manager_of_another_provider_does_not_list_offerings(self):
        response = self.get_provider_offerings(self.other_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_manager_gets_own_provider_stat_and_revenue(self):
        self.client.force_authenticate(self.manager)
        for action in ("stat", "revenue"):
            response = self.client.get(
                factories.ServiceProviderFactory.get_url(self.service_provider, action)
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)

    def test_manager_of_another_provider_is_denied_stat_and_revenue(self):
        self.client.force_authenticate(self.other_manager)
        for action in ("stat", "revenue"):
            response = self.client.get(
                factories.ServiceProviderFactory.get_url(self.service_provider, action)
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, action)

    def get_provider_tab_urls(self):
        return {
            "customers": f"/api/marketplace-service-providers/{self.service_provider.uuid.hex}/customers/",
            # Each row of the Customers tab expands into this one.
            "customer_projects": (
                f"/api/marketplace-service-providers/{self.service_provider.uuid.hex}"
                f"/customer_projects/?project_customer_uuid={self.fixture.customer.uuid.hex}"
            ),
            "compliance": factories.ServiceProviderFactory.get_compliance_url(
                self.service_provider, "compliance-overview"
            ),
            "projects": f"/api/marketplace-service-providers/{self.service_provider.uuid.hex}/projects/",
            "users": f"/api/marketplace-service-providers/{self.service_provider.uuid.hex}/users/",
        }

    def test_manager_gets_own_provider_tabs(self):
        self.client.force_authenticate(self.manager)
        for tab, url in self.get_provider_tab_urls().items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK, tab)

    def test_manager_of_another_provider_is_denied_tabs(self):
        self.client.force_authenticate(self.other_manager)
        for tab, url in self.get_provider_tab_urls().items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, tab)


class ServiceProviderManagerOrganizationVisibilityTest(test.APITestCase):
    """The manager must be able to find the provider's organization in the
    portal, but see no more of it than the provider listing already shows."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.customer = self.service_provider.customer
        self.customer.email = "billing@provider.example.com"
        self.customer.vat_code = "EE123456789"
        self.customer.save()

        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)

        self.other_manager = structure_factories.UserFactory()
        factories.ServiceProviderFactory().add_user(
            self.other_manager, ServiceProviderRole.MANAGER
        )

        self.list_url = structure_factories.CustomerFactory.get_list_url()
        self.detail_url = structure_factories.CustomerFactory.get_url(self.customer)

    def assert_restricted(self, data):
        self.assertEqual(data["uuid"], self.customer.uuid.hex)
        self.assertEqual(
            str(data["service_provider_uuid"]), self.service_provider.uuid.hex
        )
        self.assertTrue(data["is_service_provider"])
        self.assertTrue(data["is_service_provider_manager_only"])
        self.assertLessEqual(
            set(data), set(CustomerSerializer.SERVICE_PROVIDER_MANAGER_FIELDS)
        )
        for hidden in ("email", "vat_code", "projects_count", "users_count"):
            self.assertNotIn(hidden, data)

    def test_manager_lists_provider_organization_with_restricted_fields(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assert_restricted(response.data[0])

    def test_manager_finds_provider_organization_among_own_organizations(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.list_url, {"user_uuid": self.manager.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["uuid"] for item in response.data], [self.customer.uuid.hex]
        )

    def test_manager_retrieves_provider_organization_with_restricted_fields(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_restricted(response.data)

    def test_manager_of_another_provider_does_not_see_organization(self):
        self.client.force_authenticate(self.other_manager)
        response = self.client.get(self.list_url)
        self.assertNotIn(
            self.customer.uuid.hex, [item["uuid"] for item in response.data]
        )
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_visibility_grants_no_other_customer_action(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(self.detail_url, {"name": "Renamed"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        for action in ("stats", "providers", "list_users", "history"):
            response = self.client.get(f"{self.detail_url}{action}/")
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                action,
            )
        self.customer.refresh_from_db()
        self.assertNotEqual(self.customer.name, "Renamed")

    def test_manager_who_is_also_owner_gets_full_organization(self):
        self.customer.add_user(self.manager, CustomerRole.OWNER)
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], self.customer.email)
        self.assertFalse(response.data["is_service_provider_manager_only"])

    def test_staff_gets_full_organization(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["vat_code"], self.customer.vat_code)
        self.assertFalse(response.data["is_service_provider_manager_only"])

    def test_visibility_does_not_extend_to_financial_reports(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get("/api/financial-reports/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(
            self.customer.uuid.hex, [item["uuid"] for item in response.data]
        )
        response = self.client.get(f"/api/financial-reports/{self.customer.uuid.hex}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_custom_service_provider_role_grants_visibility(self):
        user = structure_factories.UserFactory()
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(ServiceProvider)
        )
        self.service_provider.add_user(user, role)
        self.client.force_authenticate(user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_restricted(response.data)

    def test_offering_role_does_not_lift_restriction(self):
        # An offering role on one of the provider's offerings confers no
        # organization visibility, so the row stays restricted and flagged.
        self.assertEqual(self.fixture.offering.customer, self.customer)
        self.fixture.offering.add_user(self.manager, OfferingRole.MANAGER)
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_restricted(response.data)


class ProviderOfferingReportsAccessTest(test.APITestCase):
    """Per-offering provider reports follow the permissions behind each action,
    held on the offering's organization or on its ServiceProvider.

    State counters and component usage accept either provider statistics or
    ORDER.LIST, the right to list that offering's orders.
    """

    STATISTICS_OR_ORDERS = {
        PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS,
        PermissionEnum.LIST_ORDERS,
    }
    # Each report, and the permissions any one of which lets it through.
    REPORTS = {
        "state_counters": STATISTICS_OR_ORDERS,
        "stats": {PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS},
        "component_stats": STATISTICS_OR_ORDERS,
        "costs": {PermissionEnum.GET_SERVICE_PROVIDER_REVENUE},
        "customers": {PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS},
    }

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.offering = self.fixture.offering

    def grant(self, role):
        for permission in set().union(*self.REPORTS.values()):
            role.add_permission(permission)

    def get_reports(self, user=None):
        if user:
            self.client.force_authenticate(user)
        return {
            action: self.client.get(
                factories.OfferingFactory.get_url(self.offering, action)
            ).status_code
            for action in self.REPORTS
        }

    def assert_all(self, codes, expected):
        self.assertEqual(codes, {action: expected for action in self.REPORTS})

    def assert_reached_with(self, codes, permission):
        """Only the reports that `permission` alone lets through answer 200."""
        self.assertEqual(
            codes,
            {
                action: status.HTTP_200_OK
                if permission in permissions
                else status.HTTP_403_FORBIDDEN
                for action, permissions in self.REPORTS.items()
            },
        )

    def test_organization_owner_gets_reports(self):
        self.grant(CustomerRole.OWNER)
        self.assert_all(
            self.get_reports(self.fixture.offering_owner), status.HTTP_200_OK
        )

    def test_service_provider_manager_gets_reports(self):
        self.grant(ServiceProviderRole.MANAGER)
        manager = structure_factories.UserFactory()
        self.service_provider.add_user(manager, ServiceProviderRole.MANAGER)
        self.assert_all(self.get_reports(manager), status.HTTP_200_OK)

    def test_each_report_needs_its_own_permission(self):
        manager = structure_factories.UserFactory()
        self.service_provider.add_user(manager, ServiceProviderRole.MANAGER)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS
        )
        self.assert_reached_with(
            self.get_reports(manager), PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS
        )

    def test_order_list_alone_reaches_state_counters_and_component_stats(self):
        manager = structure_factories.UserFactory()
        self.service_provider.add_user(manager, ServiceProviderRole.MANAGER)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        self.assert_reached_with(self.get_reports(manager), PermissionEnum.LIST_ORDERS)

    def test_provider_role_without_permission_is_denied(self):
        user = structure_factories.UserFactory()
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(ServiceProvider)
        )
        self.service_provider.add_user(user, role)
        self.assert_all(self.get_reports(user), status.HTTP_403_FORBIDDEN)

    def test_manager_of_another_provider_is_denied(self):
        self.grant(ServiceProviderRole.MANAGER)
        other_manager = structure_factories.UserFactory()
        factories.ServiceProviderFactory().add_user(
            other_manager, ServiceProviderRole.MANAGER
        )
        # The offering is outside their provider, so it is not even found.
        self.assert_all(self.get_reports(other_manager), status.HTTP_404_NOT_FOUND)

    def test_user_without_role_is_denied(self):
        self.assert_all(self.get_reports(self.fixture.user), status.HTTP_404_NOT_FOUND)

    def test_support_gets_reports(self):
        self.assert_all(
            self.get_reports(self.fixture.global_support), status.HTTP_200_OK
        )

    def get_reports_with_token(self, user, scopes):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        token = _create_pat(user, scopes=scopes, bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(token))
        return self.get_reports()

    @override_config(PAT_ENABLED=True)
    def test_token_with_report_scopes_gets_reports(self):
        self.grant(CustomerRole.OWNER)
        scopes = [
            permission.value for permission in set().union(*self.REPORTS.values())
        ]
        self.assert_all(
            self.get_reports_with_token(self.fixture.offering_owner, scopes),
            status.HTTP_200_OK,
        )

    @override_config(PAT_ENABLED=True)
    def test_token_without_report_scopes_is_denied(self):
        # Unlike the ownership check these reports used to have, the provider
        # permission is also capped by the token's scopes.
        self.grant(CustomerRole.OWNER)
        self.assert_all(
            self.get_reports_with_token(
                self.fixture.offering_owner, [PermissionEnum.UPDATE_OFFERING.value]
            ),
            status.HTTP_403_FORBIDDEN,
        )

    def assert_token_reaches(self, permission):
        self.grant(CustomerRole.OWNER)
        self.assert_reached_with(
            self.get_reports_with_token(
                self.fixture.offering_owner, [permission.value]
            ),
            permission,
        )

    # A token scoped to either statistics or ORDER.LIST keeps state counters
    # and component usage; the other reports need their own scope.
    @override_config(PAT_ENABLED=True)
    def test_statistics_token_reaches_its_reports(self):
        self.assert_token_reaches(PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS)

    @override_config(PAT_ENABLED=True)
    def test_order_list_token_reaches_its_reports(self):
        self.assert_token_reaches(PermissionEnum.LIST_ORDERS)


class ServiceProviderManagerListsTest(test.APITestCase):
    """Offering groups, campaigns and offering users a service provider manager
    can create are also listed to them, for their own provider only."""

    def setUp(self):
        from waldur_mastermind.promotions.tests import (
            factories as promotions_factories,
        )

        self.promotions_factories = promotions_factories
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.customer = self.service_provider.customer
        self.other_provider = factories.ServiceProviderFactory()

        self.manager = structure_factories.UserFactory()
        self.service_provider.add_user(self.manager, ServiceProviderRole.MANAGER)
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

        self.rows = self._create_rows(self.service_provider)
        self.other_rows = self._create_rows(self.other_provider)

    def _create_rows(self, service_provider):
        offering = factories.OfferingFactory(
            customer=service_provider.customer,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        return {
            "groups": factories.OfferingGroupFactory(
                customer=service_provider.customer
            ).uuid.hex,
            "campaigns": self.promotions_factories.CampaignFactory(
                service_provider=service_provider
            ).uuid.hex,
            "offering_users": factories.OfferingUserFactory(offering=offering).uuid.hex,
        }

    def _list(self, user):
        self.client.force_authenticate(user)
        urls = {
            "groups": factories.OfferingGroupFactory.get_list_url(),
            "campaigns": self.promotions_factories.CampaignFactory.get_list_url(),
            "offering_users": factories.OfferingUserFactory.get_list_url(),
        }
        result = {}
        for key, url in urls.items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            result[key] = {row["uuid"] for row in response.data}
        return result

    def test_manager_lists_own_provider_rows_only(self):
        listed = self._list(self.manager)
        for key, uuid in self.rows.items():
            self.assertIn(uuid, listed[key], key)
        for key, uuid in self.other_rows.items():
            self.assertNotIn(uuid, listed[key], key)

    def test_owner_lists_own_provider_rows_only(self):
        listed = self._list(self.owner)
        for key, uuid in self.rows.items():
            self.assertIn(uuid, listed[key], key)
        for key, uuid in self.other_rows.items():
            self.assertNotIn(uuid, listed[key], key)

    def test_user_without_role_lists_none(self):
        listed = self._list(structure_factories.UserFactory())
        for key in self.rows:
            self.assertEqual(listed[key], set(), key)
