"""The two API surfaces the account scope is configured and written through.

Both were reachable only in principle before these tests: the per-offering
``account_scope`` override was documented and unit-tested but never declared on
``MergedPluginOptionsSerializer``, so the API discarded it; and the provider
account endpoint carried no write permissions at all.
"""

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import AccountScopes, OfferingUserStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class OfferingAccountScopeOptionTest(test.APITestCase):
    """``account_scope`` as an offering plugin option, over the API.

    A plain ``serializers.Serializer`` drops keys it does not declare, so an
    undeclared option is not rejected -- it is accepted and silently discarded.
    That is why every case here reads the value back from the database rather
    than trusting the response status.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.offering = self.fixture.offering
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)

    def _update(self, plugin_options):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(url, {"plugin_options": plugin_options})

    def test_an_offering_can_be_put_into_provider_scope_over_the_api(self):
        response = self._update({"account_scope": AccountScopes.PROVIDER})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["account_scope"], AccountScopes.PROVIDER
        )
        self.assertEqual(self.offering.resolve_account_scope(), AccountScopes.PROVIDER)

    def test_an_offering_can_opt_out_of_its_providers_scope_over_the_api(self):
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()

        response = self._update({"account_scope": AccountScopes.OFFERING})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.resolve_account_scope(), AccountScopes.OFFERING)

    def test_omitting_the_option_leaves_the_provider_default_in_charge(self):
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()

        response = self._update({"enable_resource_access_subnets": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertNotIn("account_scope", self.offering.plugin_options)
        self.assertEqual(self.offering.resolve_account_scope(), AccountScopes.PROVIDER)

    def test_an_unrecognised_scope_is_refused_rather_than_dropped(self):
        response = self._update({"account_scope": "everywhere"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.offering.refresh_from_db()
        self.assertNotIn("account_scope", self.offering.plugin_options)


class ServiceProviderAccountWritePermissionTest(test.APITestCase):
    """Who may rewrite a shared account's username and POSIX identity.

    The endpoint declared no write permissions, so ``ActionsPermission``
    collected an empty check list and every authenticated caller whose queryset
    reached the row could PATCH it.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()
        self.offering = self.fixture.offering
        self.person = structure_factories.UserFactory()

        self.account = models.ServiceProviderAccount.objects.create(
            service_provider=self.provider,
            user=self.person,
            username="jsmith",
            state=OfferingUserStates.OK,
            backend_metadata={"uidnumber": 9001, "homeDir": "/home/jsmith"},
        )

    def _url(self):
        return factories.ServiceProviderAccountFactory.get_url(self.account)

    def _patch_username(self, user, username="renamed"):
        self.client.force_authenticate(user)
        return self.client.patch(self._url(), {"username": username})

    def test_a_member_without_the_permission_is_refused(self):
        # A project admin inside the provider's own organization: the queryset
        # reaches the row, so only the permission check can stop the write.
        response = self._patch_username(self.fixture.offering_fixture.admin)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "jsmith")

    def test_a_customer_owner_holding_the_permission_may_rename(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        response = self._patch_username(self.fixture.offering_owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "renamed")

    def test_a_provider_manager_holding_the_permission_may_rename(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING_USER)
        manager = structure_factories.UserFactory()
        self.provider.add_user(manager, ServiceProviderRole.MANAGER)
        response = self._patch_username(manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "renamed")

    def test_staff_may_rename(self):
        response = self._patch_username(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "renamed")

    def test_the_account_cannot_be_reparented(self):
        """The identity fields are read-only, so a PATCH cannot hand the
        account -- with its username and POSIX identity -- to another person or
        another provider."""
        other_provider = factories.ServiceProviderFactory()
        other_person = structure_factories.UserFactory()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self._url(),
            {
                "service_provider": factories.ServiceProviderFactory.get_url(
                    other_provider
                ),
                "user": structure_factories.UserFactory.get_url(other_person),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.service_provider, self.provider)
        self.assertEqual(self.account.user, self.person)


class ServiceProviderAccountDeletionTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()
        self.person = structure_factories.UserFactory()
        self.account = models.ServiceProviderAccount.objects.create(
            service_provider=self.provider,
            user=self.person,
            username="jsmith",
            state=OfferingUserStates.OK,
        )
        self.client.force_authenticate(self.fixture.staff)

    def _url(self):
        return factories.ServiceProviderAccountFactory.get_url(self.account)

    def test_deletion_is_refused_while_offering_accounts_read_through_it(self):
        """RESTRICT would refuse this at the database with a 500; the validator
        turns it into a 400 that names what is in the way."""
        offering_user = models.OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.person,
            service_provider_account=self.account,
        )
        response = self.client.delete(self._url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            models.ServiceProviderAccount.objects.filter(pk=self.account.pk).exists()
        )
        self.assertTrue(
            models.OfferingUser.objects.filter(pk=offering_user.pk).exists()
        )

    def test_deletion_succeeds_once_nothing_reads_through_it(self):
        response = self.client.delete(self._url())
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ServiceProviderAccount.objects.filter(pk=self.account.pk).exists()
        )


class ServiceProviderAccountListQueryCountTest(test.APITestCase):
    """The list page must not cost a COUNT per row."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.client.force_authenticate(self.fixture.staff)

    def _create_accounts(self, count):
        for _ in range(count):
            person = structure_factories.UserFactory()
            account = models.ServiceProviderAccount.objects.create(
                service_provider=self.provider,
                user=person,
                state=OfferingUserStates.OK,
            )
            models.OfferingUser.objects.create(
                offering=self.fixture.offering,
                user=person,
                service_provider_account=account,
            )

    def _measure(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx.captured_queries)

    def test_offering_count_is_reported_and_flat(self):
        url = factories.ServiceProviderAccountFactory.get_list_url()
        self._create_accounts(1)
        one = self._measure(url)
        self._create_accounts(4)
        five = self._measure(url)

        response = self.client.get(url)
        self.assertEqual(len(response.data), 5)
        self.assertTrue(all(row["offering_count"] == 1 for row in response.data))
        self.assertEqual(
            one,
            five,
            "the account list issues a query per row -- offering_count is not annotated",
        )
