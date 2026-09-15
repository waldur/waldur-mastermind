"""What a provider's account settings produce.

The provider accounts API follows the offerings' attribute exposure, adoption
refuses usernames that cannot survive, a preview shows the effect of a change
without making it, and the shared accounts render as one GLAuth directory.
"""

import tomllib

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    AccountScopes,
    OfferingUserStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

POLICY = utils.UsernameGenerationPolicy
POSIX_METADATA = {
    "uidnumber": 9001,
    "primarygroup": 9001,
    "loginShell": "/bin/bash",
    "homeDir": "/home/hpc_9001",
}


class ProviderAccountAttributeExposureTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.person = structure_factories.UserFactory()
        self.account = models.ServiceProviderAccount.objects.create(
            service_provider=self.provider,
            user=self.person,
            username="hpc_9001",
            state=OfferingUserStates.OK,
        )
        self.offering_a = self.fixture.offering
        self.offering_b = factories.OfferingFactory(customer=self.provider.customer)
        self.client.force_authenticate(self.fixture.staff)

    def _back(self, offering):
        models.OfferingUser.objects.create(
            offering=offering, user=self.person, service_provider_account=self.account
        )

    def _account(self):
        response = self.client.get(
            factories.ServiceProviderAccountFactory.get_url(self.account)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_attributes_every_offering_exposes_are_shown(self):
        self._back(self.offering_a)
        self._back(self.offering_b)

        data = self._account()

        self.assertEqual(data["user_full_name"], self.person.full_name)
        self.assertEqual(data["user_email"], self.person.email)

    def test_an_attribute_one_offering_keeps_is_hidden(self):
        self._back(self.offering_a)
        self._back(self.offering_b)
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering_b, expose_email=False
        )

        data = self._account()

        self.assertIsNone(data["user_email"])
        self.assertEqual(data["user_full_name"], self.person.full_name)

    def test_an_account_backing_no_offering_shows_no_attributes(self):
        data = self._account()

        self.assertIsNone(data["user_username"])
        self.assertIsNone(data["user_full_name"])
        self.assertIsNone(data["user_email"])


class AdoptionValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.offering_a = self.fixture.offering
        self.offering_b = factories.OfferingFactory(customer=self.provider.customer)
        self.person = structure_factories.UserFactory()
        self.client.force_authenticate(self.fixture.service_owner)

    def _account(self, offering, username, user=None):
        return models.OfferingUser.objects.create(
            offering=offering,
            user=user or self.person,
            username=username,
            state=OfferingUserStates.OK,
        )

    def _adopt(self, resolutions=None):
        return self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.provider, action="adopt_provider_accounts"
            ),
            {"resolutions": resolutions or {}},
        )

    def test_a_resolution_that_is_not_a_candidate_is_refused(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "j.smith")

        response = self._adopt({self.person.uuid.hex: "someone.else"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resolutions", response.data)
        self.assertFalse(models.ServiceProviderAccount.objects.exists())

    def test_two_people_with_one_username_are_refused(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "jsmith", user=structure_factories.UserFactory())

        response = self._adopt()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["usernames"], ["jsmith"])
        self.assertFalse(models.ServiceProviderAccount.objects.exists())

    def test_a_name_held_by_another_provider_account_is_refused(self):
        models.ServiceProviderAccount.objects.create(
            service_provider=self.provider,
            user=structure_factories.UserFactory(),
            username="jsmith",
        )
        self._account(self.offering_a, "jsmith")

        response = self._adopt()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.ServiceProviderAccount.objects.count(), 1)

    def test_a_valid_resolution_still_adopts(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "j.smith")

        response = self._adopt({self.person.uuid.hex: "jsmith"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            models.ServiceProviderAccount.objects.get(user=self.person).username,
            "jsmith",
        )


class AccountOptionsPreviewTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.offering = self.fixture.offering
        self.offering.type = BASIC_OFFERING
        self.offering.save()
        self.person = structure_factories.UserFactory()
        self.client.force_authenticate(self.fixture.service_owner)

    def _account(self, username, offering=None, **kwargs):
        return models.OfferingUser.objects.create(
            offering=offering or self.offering,
            user=self.person,
            username=username,
            state=OfferingUserStates.OK,
            **kwargs,
        )

    def _preview(self, account_options):
        response = self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.provider, action="account_options_preview"
            ),
            {"account_options": account_options},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def _offering(self, preview, offering=None):
        uuid = (offering or self.offering).uuid.hex
        return next(o for o in preview["offerings"] if o["uuid"] == uuid)

    def test_lists_the_renames_an_offering_account_would_get(self):
        account = self._account("old_name")

        preview = self._preview(
            {"username_generation_policy": POLICY.WALDUR_USERNAME.value}
        )

        report = self._offering(preview)
        self.assertIn("username_generation_policy", report["changed"])
        self.assertEqual(
            report["settings"]["username_generation_policy"]["after"],
            {"value": POLICY.WALDUR_USERNAME.value, "source": "provider"},
        )
        self.assertEqual(
            report["renames"],
            [
                {
                    "username": "old_name",
                    "new_username": self.person.username,
                    "home_directory": "",
                    "new_home_directory": f"/home/{self.person.username}",
                }
            ],
        )
        self.assertEqual(preview["renamed"], 1)
        # Nothing is written.
        account.refresh_from_db()
        self.assertEqual(account.username, "old_name")
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.account_options, {})

    def test_an_offering_overriding_the_setting_is_not_affected(self):
        self.offering.plugin_options = {
            "username_generation_policy": POLICY.FULL_NAME.value
        }
        self.offering.save()
        self._account("old_name")

        preview = self._preview(
            {"username_generation_policy": POLICY.WALDUR_USERNAME.value}
        )

        report = self._offering(preview)
        self.assertNotIn("username_generation_policy", report["changed"])
        self.assertEqual(report["renames"], [])

    def test_the_anonymized_preview_allocates_nothing(self):
        pool = factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_uid=9000,
            max_uid=9019,
            next_uid=9000,
            min_gid=9000,
            max_gid=9019,
            next_gid=9000,
        )
        self._account("old_name")

        preview = self._preview(
            {
                "username_generation_policy": POLICY.ANONYMIZED.value,
                "username_anonymized_prefix": "hpc_",
            }
        )

        report = self._offering(preview)
        # The person holds no UID yet, so the rename would allocate one first.
        self.assertIsNone(report["renames"][0]["new_username"])
        self.assertEqual(report["example"]["username"], "hpc_9000")
        self.assertFalse(models.PosixIdentity.objects.exists())
        pool.refresh_from_db()
        self.assertEqual(pool.next_uid, 9000)

    def test_provider_accounts_keep_their_names(self):
        self.provider.account_options = {"account_scope": AccountScopes.PROVIDER}
        self.provider.save()
        backing = models.ServiceProviderAccount.objects.create(
            service_provider=self.provider,
            user=self.person,
            username="hpc_9001",
            state=OfferingUserStates.OK,
        )
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.person,
            service_provider_account=backing,
            state=OfferingUserStates.OK,
        )

        preview = self._preview(
            {"username_generation_policy": POLICY.WALDUR_USERNAME.value}
        )

        report = self._offering(preview)
        self.assertEqual(report["renames"], [])
        self.assertEqual(report["provider_accounts_kept"], 1)
        self.assertEqual(preview["provider_accounts_kept"], 1)

    def test_home_and_shell_apply_to_new_accounts_only(self):
        self._account("jsmith")

        preview = self._preview({"login_shell": "/bin/zsh"})

        report = self._offering(preview)
        self.assertEqual(report["renames"], [])
        self.assertEqual(report["accounts_keeping_home_or_shell"], 1)
        self.assertEqual(report["example"]["login_shell"], "/bin/zsh")

    def test_a_blank_value_previews_removing_the_setting(self):
        self.provider.account_options = {"login_shell": "/bin/zsh"}
        self.provider.save()

        preview = self._preview({"login_shell": ""})

        self.assertEqual(preview["account_options"]["proposed"], {})
        self.assertEqual(
            self._offering(preview)["settings"]["login_shell"]["after"],
            {"value": "/bin/bash", "source": "default"},
        )

    def test_switching_to_provider_scope_reports_conflicts(self):
        other = factories.OfferingFactory(
            customer=self.provider.customer, type=BASIC_OFFERING
        )
        self._account("jsmith")
        self._account("j.smith", offering=other)

        preview = self._preview({"account_scope": AccountScopes.PROVIDER})

        self.assertEqual(preview["username_conflicts"], 1)

    def test_an_unsafe_path_is_refused(self):
        response = self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.provider, action="account_options_preview"
            ),
            {"account_options": {"login_shell": "/bin/sh; rm -rf /"}},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_owners_may_preview(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.provider, action="account_options_preview"
            ),
            {"account_options": {"login_shell": "/bin/zsh"}},
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


class ProviderGlauthTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_options = {"account_scope": AccountScopes.PROVIDER}
        self.provider.save()
        self.offerings = [
            self.fixture.offering,
            factories.OfferingFactory(customer=self.provider.customer),
        ]
        for offering in self.offerings:
            offering.type = BASIC_OFFERING
            offering.plugin_options = {
                "service_provider_can_create_offering_user": True
            }
            offering.save()
        self.person = structure_factories.UserFactory()
        for offering in self.offerings:
            models.OfferingUser.objects.create(
                offering=offering,
                user=self.person,
                username="hpc_9001",
                backend_metadata=dict(POSIX_METADATA),
                state=OfferingUserStates.OK,
            )
        self.client.force_authenticate(self.fixture.service_owner)

    def _get(self, action):
        response = self.client.get(
            factories.ServiceProviderFactory.get_url(self.provider, action=action)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response

    def _config(self):
        response = self._get("glauth_users_config")
        text = response.content.decode()
        return text, tomllib.loads(text)

    def test_each_person_is_listed_once(self):
        _, config = self._config()

        self.assertEqual(
            [user["name"] for user in config["users"]].count("hpc_9001"), 1
        )
        self.assertEqual(
            [group["gidnumber"] for group in config["groups"]].count(9001), 1
        )

    def test_the_tree_covers_every_sharing_offering(self):
        tree = self._get("glauth_tree").data

        self.assertEqual(
            {offering["uuid"] for offering in tree["offerings"]},
            {offering.uuid.hex for offering in self.offerings},
        )
        self.assertEqual([user["username"] for user in tree["users"]], ["hpc_9001"])
        self.assertEqual(tree["warnings"], [])

    def test_offerings_keeping_their_own_accounts_are_left_out(self):
        separate = factories.OfferingFactory(
            customer=self.provider.customer,
            type=BASIC_OFFERING,
            plugin_options={
                "account_scope": AccountScopes.OFFERING,
                "service_provider_can_create_offering_user": True,
            },
        )
        models.OfferingUser.objects.create(
            offering=separate,
            user=structure_factories.UserFactory(),
            username="elsewhere",
            backend_metadata={
                **POSIX_METADATA,
                "uidnumber": 9002,
                "primarygroup": 9002,
            },
            state=OfferingUserStates.OK,
        )

        tree = self._get("glauth_tree").data

        self.assertNotIn(separate.uuid.hex, [o["uuid"] for o in tree["offerings"]])
        self.assertEqual([user["username"] for user in tree["users"]], ["hpc_9001"])

    def test_different_shared_passwords_are_reported(self):
        for offering, password in zip(self.offerings, ("first", "second")):
            offering.secret_options = {"shared_user_password": password}
            offering.save()

        text, config = self._config()

        self.assertIn("# The offerings set different shared user passwords", text)
        self.assertNotIn("passsha256", config["users"][0])

    def test_people_without_a_provider_role_cannot_view(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get(
            factories.ServiceProviderFactory.get_url(
                self.provider, action="glauth_tree"
            )
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
