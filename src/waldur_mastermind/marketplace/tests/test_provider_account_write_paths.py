"""Every path that writes an offering account, against a provider-backed one.

The review found that the guard added to OfferingUserSerializer covered only
the API path, while a dozen older paths wrote username straight onto the model.
These cover the refusal, the callers that must not reach it, and the creators
that must produce a backed row in the first place.
"""

from django.core.exceptions import ValidationError
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import AccountScopes, OfferingUserStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ProviderScopedFixture(test.APITestCase):
    """A provider in provider scope, one offering, one user with a backed account."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()
        self.offering = self.fixture.offering
        self.user = structure_factories.UserFactory()

    def backed(self):
        account = utils.get_or_create_provider_account(self.user, self.offering)
        account.username = "jsmith"
        account.state = OfferingUserStates.OK
        account.save()
        offering_user = models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            service_provider_account=account,
        )
        return account, offering_user


class DelegatedWriteRefusalTest(ProviderScopedFixture):
    def test_a_delegated_write_on_an_existing_row_is_refused(self):
        _, offering_user = self.backed()
        offering_user.username = "someone-else"

        with self.assertRaises(ValidationError):
            offering_user.save()

    def test_the_refusal_names_the_owning_account(self):
        account, offering_user = self.backed()
        offering_user.username = "someone-else"

        with self.assertRaises(ValidationError) as caught:
            offering_user.save()
        self.assertIn(account.uuid.hex, str(caught.exception))

    def test_a_new_backed_row_takes_the_parents_values_without_refusing(self):
        """Insert must not raise: the creator builds the row before pulling."""
        account = utils.get_or_create_provider_account(self.user, self.offering)
        account.username = "jsmith"
        account.save()

        offering_user = models.OfferingUser.objects.create(
            offering=self.offering, user=self.user, service_provider_account=account
        )

        self.assertEqual(offering_user.username, "jsmith")

    def test_an_unrelated_field_still_saves(self):
        """Only the delegated columns are refused; the association's own are not."""
        _, offering_user = self.backed()
        offering_user.state = OfferingUserStates.OK

        offering_user.save(update_fields=["state"])

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_propagation_from_the_parent_is_not_refused(self):
        account, offering_user = self.backed()
        account.username = "j.smith"
        account.save()

        utils.propagate_provider_account(account)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "j.smith")

    def test_an_unbacked_account_is_still_writable(self):
        other = factories.OfferingFactory()
        offering_user = models.OfferingUser.objects.create(
            offering=other, user=self.user, username="free"
        )
        offering_user.username = "changed"

        offering_user.save()

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "changed")


class BulkPathsSkipBackedAccountsTest(ProviderScopedFixture):
    """The bulk writers must exclude backed rows rather than hit the refusal.

    Each of these iterates many accounts; one backed row raising would abort the
    whole run for everyone else.
    """

    def test_refresh_offering_usernames_leaves_a_backed_account_alone(self):
        account, offering_user = self.backed()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.OfferingFactory.get_url(
                self.offering, "refresh_offering_usernames"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, account.username)


class SetOfferingsUsernameTest(ProviderScopedFixture):
    def setUp(self):
        super().setUp()
        # The action only touches offerings the user actually consumes, so the
        # account under test needs a resource to be in scope.
        self.user = self.fixture.admin
        self.resource = self.fixture.resource
        CustomerRole.OWNER.add_permission(
            PermissionEnum.SET_SERVICE_PROVIDER_OFFERINGS_USERNAME
        )
        self.url = factories.ServiceProviderFactory.get_url(
            self.provider, action="set_offerings_username"
        )

    def test_it_refuses_a_backed_account_with_a_clear_error(self):
        account, _ = self.backed()
        self.client.force_login(self.fixture.offering_owner)

        response = self.client.post(
            self.url, {"user_uuid": self.user.uuid, "username": "override"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        account.refresh_from_db()
        self.assertEqual(account.username, "jsmith")

    def test_it_still_sets_the_username_on_an_unbacked_account(self):
        self.provider.account_scope = AccountScopes.OFFERING
        self.provider.save()
        self.client.force_login(self.fixture.offering_owner)

        response = self.client.post(
            self.url, {"user_uuid": self.user.uuid, "username": "override"}
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering_user = models.OfferingUser.objects.get(
            user=self.user, offering=self.offering
        )
        self.assertEqual(offering_user.username, "override")


class ProviderAwareCreatorTest(ProviderScopedFixture):
    """Every creator must produce a BACKED row under provider scope.

    A row created unbacked stays unbacked until someone runs an adoption, which
    is the divergence provider scope exists to remove.
    """

    def test_the_shared_creator_backs_the_row(self):
        offering_user, created = utils.create_offering_user(self.user, self.offering)

        self.assertTrue(created)
        self.assertTrue(offering_user.is_provider_backed)

    def test_a_caller_supplied_username_is_ignored_under_provider_scope(self):
        offering_user, _ = utils.create_offering_user(
            self.user, self.offering, username="from-the-caller"
        )

        self.assertTrue(offering_user.is_provider_backed)
        self.assertNotEqual(offering_user.username, "from-the-caller")

    def test_a_caller_supplied_username_is_kept_outside_provider_scope(self):
        self.provider.account_scope = AccountScopes.OFFERING
        self.provider.save()

        offering_user, _ = utils.create_offering_user(
            self.user, self.offering, username="from-the-caller"
        )

        self.assertFalse(offering_user.is_provider_backed)
        self.assertEqual(offering_user.username, "from-the-caller")

    def test_calling_it_twice_returns_the_same_row(self):
        first, created_first = utils.create_offering_user(self.user, self.offering)
        second, created_second = utils.create_offering_user(self.user, self.offering)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)


class ReviewFindingsTest(ProviderScopedFixture):
    """The write paths a reviewer found after the refusal landed.

    Making save() refuse turned two previously-silent writes into crashes, and
    the invariant "every creator goes through create_offering_user" turned out
    to have two holes. Each of these is one of those.
    """

    def test_set_posix_attributes_writes_through_to_the_parent(self):
        """It wrote the cache first, so the refusal fired before the parent
        was ever updated and the write-through never ran."""
        factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_uid=100000,
            max_uid=199999,
            next_uid=100000,
            min_gid=200000,
            max_gid=299999,
            next_gid=200000,
        )
        account, offering_user = self.backed()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingUserFactory.get_url(
            offering_user, "set-posix-attributes"
        )

        response = self.client.post(url, {"uidnumber": 100500}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        offering_user.refresh_from_db()
        self.assertEqual(account.backend_metadata.get("uidnumber"), 100500)
        self.assertEqual(
            offering_user.backend_metadata.get("uidnumber"),
            account.backend_metadata.get("uidnumber"),
        )

    def test_the_creator_adopts_an_existing_unbacked_row(self):
        """get_or_create defaults apply only on insert, so a row predating
        provider scope stayed unbacked for ever."""
        existing = models.OfferingUser.objects.create(
            offering=self.offering, user=self.user, username="from_before"
        )

        offering_user, created = utils.create_offering_user(self.user, self.offering)

        self.assertFalse(created)
        self.assertEqual(offering_user.pk, existing.pk)
        self.assertTrue(offering_user.is_provider_backed)

    def test_the_api_create_route_produces_a_backed_row(self):
        """The one public creator that did not go through create_offering_user."""
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.OfferingUserFactory.get_list_url(),
            {
                "offering": factories.OfferingFactory.get_url(self.offering),
                "user": structure_factories.UserFactory.get_url(self.user),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering_user = models.OfferingUser.objects.get(
            offering=self.offering, user=self.user
        )
        self.assertTrue(offering_user.is_provider_backed)


class ProviderScopeAdoptionTest(ProviderScopedFixture):
    def test_enabling_provider_scope_adopts_existing_accounts(self):
        """Validation only refused an ambiguous flip; nothing acted on a clean
        one, so existing accounts stayed per-offering while new ones were
        backed -- the mixed state the setting exists to remove."""
        self.provider.account_scope = AccountScopes.OFFERING
        self.provider.save()
        existing = models.OfferingUser.objects.create(
            offering=self.offering, user=self.user, username="jsmith"
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.patch(
            factories.ServiceProviderFactory.get_url(self.provider),
            {"account_scope": AccountScopes.PROVIDER},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        existing.refresh_from_db()
        self.assertTrue(existing.is_provider_backed)


class ProviderAccountHomeDirTest(ProviderScopedFixture):
    def test_a_second_offering_does_not_move_the_shared_home_directory(self):
        """setup_linux_related_data rewrites homeDir from the offering's own
        prefix, so running it per offering let the second one move a shared
        account's home and discard an operator's override."""
        account = utils.get_or_create_provider_account(self.user, self.offering)
        account.username = "jsmith"
        account.backend_metadata = {"uidnumber": 9001, "homeDir": "/operator/override"}
        account.save()

        other = factories.OfferingFactory(customer=self.provider.customer)
        other.plugin_options = {"homedir_prefix": "/elsewhere/"}
        other.save()
        utils.get_or_create_provider_account(self.user, other)

        account.refresh_from_db()
        self.assertEqual(account.backend_metadata["homeDir"], "/operator/override")


class ProviderAccountReleaseTest(ProviderScopedFixture):
    def test_the_agents_deletion_path_releases_the_provider_account(self):
        """Release was wired only to the role-lost task, so an agent tearing
        associations down through the offering-user actions left the provider
        account in OK with no live readers."""
        account, offering_user = self.backed()
        models.OfferingUser.objects.filter(pk=offering_user.pk).update(
            state=OfferingUserStates.OK
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.OfferingUserFactory.get_url(offering_user, "request-deletion")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account.refresh_from_db()
        self.assertEqual(account.state, OfferingUserStates.DELETION_REQUESTED)
