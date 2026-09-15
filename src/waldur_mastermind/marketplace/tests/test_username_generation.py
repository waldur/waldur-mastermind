from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_freeipa.tests import factories as freeipa_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class UsernameGenerationTest(TestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.user = self.fixture.user

    def test_username_is_empty_for_service_provider_case(self):
        self.offering.plugin_options = {
            "username_generation_policy": "service_provider"
        }
        self.offering.save()

        username = utils.generate_username(self.user, self.offering)

        self.assertEqual(username, "")

    def test_username_generation_for_anonymized_case(self):
        """With a pool the name is the prefix plus the UID the pool hands out."""
        self.offering.plugin_options = {
            "username_generation_policy": "anonymized",
            "username_anonymized_prefix": "anonymized_test_",
        }
        self.offering.save()
        factories.PosixIdPoolFactory(
            offering=self.offering, min_uid=9000, max_uid=9999, next_uid=9000
        )

        username = utils.generate_username(self.user, self.offering)
        self.assertEqual(username, "anonymized_test_9000")

        other = structure_factories.UserFactory()
        self.assertEqual(
            utils.generate_username(other, self.offering), "anonymized_test_9001"
        )

    def test_anonymized_username_is_stable_for_the_same_person(self):
        """Allocation is idempotent per user, so regenerating gives the same name."""
        self.offering.plugin_options = {
            "username_generation_policy": "anonymized",
            "username_anonymized_prefix": "anon_",
        }
        self.offering.save()
        factories.PosixIdPoolFactory(offering=self.offering, next_uid=100005)

        first = utils.generate_username(self.user, self.offering)
        second = utils.generate_username(self.user, self.offering)
        self.assertEqual(first, "anon_100005")
        self.assertEqual(first, second)
        self.assertEqual(
            marketplace_models.PosixIdentity.objects.filter(user=self.user).count(), 1
        )

    def test_anonymized_username_falls_back_to_the_counter_without_a_pool(self):
        self.offering.plugin_options = {
            "username_generation_policy": "anonymized",
            "username_anonymized_prefix": "anonymized_test_",
        }
        self.offering.save()

        with self.assertLogs("waldur_mastermind.marketplace.utils", "WARNING") as logs:
            username0 = utils.generate_username(self.user, self.offering)
        self.assertEqual(username0, "anonymized_test_00000")
        self.assertIn("No POSIX UID resolves", logs.output[0])
        marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username=username0,
        )

        username1 = utils.generate_username(self.user, self.offering)
        self.assertEqual(username1, "anonymized_test_00001")

    def test_anonymized_username_uses_the_user_attribute_uid_when_so_sourced(self):
        self.offering.plugin_options = {
            "username_generation_policy": "anonymized",
            "username_anonymized_prefix": "claim_",
            "uid_source": "user_attribute",
        }
        self.offering.save()
        # A pool exists but must not be consulted for this offering.
        factories.PosixIdPoolFactory(offering=self.offering)
        self.user.uid_number = 424242
        self.user.save()

        self.assertEqual(
            utils.generate_username(self.user, self.offering), "claim_424242"
        )
        self.assertFalse(marketplace_models.PosixIdentity.objects.exists())

    def test_anonymized_username_reads_the_uid_already_on_the_account(self):
        """A recorded (possibly overridden) UID wins over a fresh allocation."""
        self.offering.plugin_options = {
            "username_generation_policy": "anonymized",
            "username_anonymized_prefix": "anon_",
        }
        self.offering.save()
        factories.PosixIdPoolFactory(offering=self.offering)
        offering_user = factories.OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="anon_777",
            backend_metadata={"uidnumber": 777},
        )

        self.assertEqual(
            utils.generate_username(self.user, self.offering, offering_user),
            "anon_777",
        )

    def test_anonymized_prefix_is_inherited_from_the_provider(self):
        provider = self.fixture.service_provider
        provider.account_options["username_anonymized_prefix"] = "hpc_"
        provider.save()
        self.offering.plugin_options = {"username_generation_policy": "anonymized"}
        self.offering.save()
        factories.PosixIdPoolFactory(
            service_provider=provider, min_uid=9000, max_uid=9999, next_uid=9001
        )

        self.assertEqual(utils.generate_username(self.user, self.offering), "hpc_9001")

    def test_resolver_and_generator_share_one_default_prefix(self):
        self.assertEqual(
            marketplace_models.Offering.ACCOUNT_SETTING_DEFAULTS[
                "username_anonymized_prefix"
            ],
            utils.DEFAULT_ANONYMIZED_PREFIX,
        )
        self.offering.plugin_options = {"username_generation_policy": "anonymized"}
        self.offering.save()
        factories.PosixIdPoolFactory(offering=self.offering, next_uid=100001)

        self.assertEqual(
            utils.generate_username(self.user, self.offering),
            f"{utils.DEFAULT_ANONYMIZED_PREFIX}100001",
        )

    def test_username_generation_for_full_name_case(self):
        self.offering.plugin_options = {
            "username_generation_policy": "full_name",
        }
        self.offering.save()
        self.user.first_name = "Jöhn Karlos"
        self.user.last_name = "Doe Jr"
        self.user.save()

        username0 = utils.generate_username(self.user, self.offering)
        marketplace_models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.user,
            username=username0,
        )

        self.assertEqual(username0, "john_karlos_doe_jr_00")

        username1 = utils.generate_username(self.user, self.offering)

        self.assertEqual(username1, "john_karlos_doe_jr_01")

    def test_username_generation_for_waldur_username(self):
        self.offering.plugin_options = {
            "username_generation_policy": "waldur_username",
        }
        self.offering.save()

        username = utils.generate_username(self.user, self.offering)

        self.assertEqual(username, self.user.username)

    def test_username_generation_for_freeipa(self):
        self.offering.plugin_options = {
            "username_generation_policy": "freeipa",
        }
        self.offering.save()

        profile = freeipa_factories.ProfileFactory(user=self.user)

        username = utils.generate_username(self.user, self.offering)

        self.assertEqual(username, profile.username)


class AnonymizedUsernameAcrossOfferingsTest(test.APITestCase):
    """Two offerings of one provider drawing UIDs from the provider's pool."""

    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_options["username_anonymized_prefix"] = "hpc_"
        self.provider.account_options["username_generation_policy"] = "anonymized"
        self.provider.save()
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_uid=9000,
            max_uid=9999,
            next_uid=9001,
            min_gid=9000,
            max_gid=9999,
            next_gid=9001,
        )
        self.offering_a = self.fixture.offering
        self.offering_a.type = SITE_AGENT_OFFERING
        self.offering_a.save()
        self.offering_b = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING, customer=self.provider.customer
        )
        self.user = structure_factories.UserFactory()

    def _set_scope(self, scope):
        self.provider.account_options["account_scope"] = scope
        self.provider.save()

    def test_provider_scope_names_one_account_after_the_uid(self):
        self._set_scope("provider")

        first, _ = utils.create_offering_user(self.user, self.offering_a)
        second, _ = utils.create_offering_user(self.user, self.offering_b)

        accounts = marketplace_models.ServiceProviderAccount.objects.filter(
            service_provider=self.provider, user=self.user
        )
        self.assertEqual(accounts.count(), 1)
        account = accounts.get()
        self.assertEqual(account.username, "hpc_9001")
        self.assertEqual(account.backend_metadata["uidnumber"], 9001)
        self.assertEqual(first.username, "hpc_9001")
        self.assertEqual(second.username, "hpc_9001")
        self.assertEqual(
            marketplace_models.PosixIdentity.objects.filter(user=self.user).count(), 1
        )

    def test_uid_matches_the_number_in_the_username(self):
        self._set_scope("provider")
        offering_user, _ = utils.create_offering_user(self.user, self.offering_a)
        prefix = "hpc_"
        self.assertEqual(
            offering_user.backend_metadata["uidnumber"],
            int(offering_user.username[len(prefix) :]),
        )
        self.assertEqual(offering_user.backend_metadata["homeDir"], "/home/hpc_9001")

    def test_two_people_entering_through_different_offerings_never_collide(self):
        self._set_scope("provider")
        other = structure_factories.UserFactory()

        via_a, _ = utils.create_offering_user(self.user, self.offering_a)
        via_b, _ = utils.create_offering_user(other, self.offering_b)

        self.assertEqual(via_a.username, "hpc_9001")
        self.assertEqual(via_b.username, "hpc_9002")

    def test_offering_scope_with_a_shared_pool_gives_the_same_name_everywhere(self):
        self._set_scope("offering")

        on_a, _ = utils.create_offering_user(self.user, self.offering_a)
        on_b, _ = utils.create_offering_user(self.user, self.offering_b)

        self.assertFalse(on_a.is_provider_backed)
        self.assertEqual(on_a.username, "hpc_9001")
        self.assertEqual(on_b.username, "hpc_9001")
        self.assertEqual(on_a.backend_metadata["uidnumber"], 9001)
        self.assertEqual(on_b.backend_metadata["uidnumber"], 9001)
        self.assertEqual(
            marketplace_models.PosixIdentity.objects.filter(user=self.user).count(), 1
        )

    def test_refresh_offering_usernames_keeps_derived_names(self):
        self._set_scope("offering")
        offering_user, _ = utils.create_offering_user(self.user, self.offering_a)
        self.assertEqual(offering_user.username, "hpc_9001")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.OfferingFactory.get_url(
                self.offering_a, "refresh_offering_usernames"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "hpc_9001")
        self.assertEqual(
            self.pool.__class__.objects.get(pk=self.pool.pk).next_uid, 9002
        )

    def test_policy_change_handler_keeps_derived_names(self):
        self._set_scope("offering")
        self.offering_a.plugin_options = {"username_generation_policy": "full_name"}
        self.offering_a.save()
        offering_user, _ = utils.create_offering_user(self.user, self.offering_a)
        uid = offering_user.backend_metadata["uidnumber"]

        self.offering_a.plugin_options = {"username_generation_policy": "anonymized"}
        self.offering_a.save()
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, f"hpc_{uid}")

        # A second save with the policy unchanged is a pure no-op.
        self.offering_a.plugin_options = {
            "username_generation_policy": "anonymized",
            "homedir_prefix": "/home/",
        }
        self.offering_a.save()
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, f"hpc_{uid}")
        self.assertEqual(offering_user.backend_metadata["uidnumber"], uid)
