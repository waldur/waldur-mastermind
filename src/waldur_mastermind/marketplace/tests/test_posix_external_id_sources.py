"""Offerings that source a POSIX identifier from the user, not from the pool.

``uid_source`` / ``gid_source`` are per-offering while the pool is normally
per-provider, so one provider can legally run offering A on pool-allocated UIDs
and offering B on an OIDC claim. Nothing that shares an identity across the
provider — a pin, the retrofit, the re-point action — may write a pool value
over the externally sourced one.
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, posix_ids, utils
from waldur_mastermind.marketplace.tests import factories


class ExternalUidSourceTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        # A: UID and GID from the pool. B: UID from the user's claim, GID pooled.
        self.pooled_offering = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.claim_offering = factories.OfferingFactory(
            customer=self.customer,
            name="Cluster B",
            plugin_options={"uid_source": "user_attribute"},
        )
        self.user = structure_factories.UserFactory(uid_number=777000)
        self.pooled_account = self.account(self.pooled_offering)
        self.claim_account = self.account(self.claim_offering)

    def account(self, offering):
        offering_user = factories.OfferingUserFactory(offering=offering, user=self.user)
        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save()
        return offering_user

    def test_claim_account_keeps_the_external_uid(self):
        self.assertEqual(self.claim_account.backend_metadata["uidnumber"], 777000)
        self.assertEqual(self.pooled_account.backend_metadata["uidnumber"], 100000)

    def test_claim_account_still_draws_its_gid_from_the_pool(self):
        self.assertEqual(self.claim_account.backend_metadata["primarygroup"], 200000)

    def test_allocate_refuses_an_externally_sourced_namespace(self):
        self.assertIsNone(
            posix_ids.allocate(self.claim_offering, posix_ids.UID, self.claim_account)
        )

    def test_pin_does_not_overwrite_the_external_uid(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingUserFactory.get_url(
            self.pooled_account, action="set-posix-attributes"
        )
        response = self.client.post(url, {"uidnumber": 100050})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.pooled_account.refresh_from_db()
        self.claim_account.refresh_from_db()
        self.assertEqual(self.pooled_account.backend_metadata["uidnumber"], 100050)
        self.assertEqual(self.claim_account.backend_metadata["uidnumber"], 777000)

    def test_pin_of_a_pooled_namespace_still_reaches_the_claim_account(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingUserFactory.get_url(
            self.pooled_account, action="set-posix-attributes"
        )
        self.client.post(url, {"primarygroup": 200050})

        self.claim_account.refresh_from_db()
        self.assertEqual(self.claim_account.backend_metadata["primarygroup"], 200050)
        self.assertEqual(self.claim_account.backend_metadata["uidnumber"], 777000)

    def test_repoint_does_not_allocate_an_externally_sourced_uid(self):
        override = factories.PosixIdPoolFactory(
            offering=self.claim_offering,
            min_uid=500000,
            max_uid=500099,
            next_uid=500000,
            min_gid=600000,
            max_gid=600099,
            next_gid=600000,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.PosixIdPoolFactory.get_url(override, action="repoint"),
            {"confirm": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.claim_account.refresh_from_db()
        self.assertEqual(self.claim_account.backend_metadata["uidnumber"], 777000)
        self.assertEqual(self.claim_account.backend_metadata["primarygroup"], 600000)
        namespaces = {row["namespace"] for row in response.data["changes"]}
        self.assertEqual(namespaces, {"gid"})


class DisabledPosixAccountTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.offering = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.plain_offering = factories.OfferingFactory(
            customer=self.customer,
            name="Object storage",
            plugin_options={"enable_posix_account": False},
        )
        self.user = structure_factories.UserFactory()

    def test_report_does_not_attribute_a_pool_to_an_opted_out_offering(self):
        offering_user = factories.OfferingUserFactory(
            offering=self.offering, user=self.user
        )
        utils.setup_linux_related_data(offering_user, self.offering)
        offering_user.save()
        # Metadata predating the opt-out, so the row is reported at all.
        factories.OfferingUserFactory(
            offering=self.plain_offering,
            user=self.user,
            backend_metadata={"uidnumber": 100000},
        )

        rows = utils.get_user_posix_identities(
            models.OfferingUser.objects.filter(user=self.user).select_related(
                "offering"
            )
        )
        by_offering = {
            row["offerings"][0]["name"]: row
            for row in rows
            if row["namespace"] == "uid"
        }
        # The opted-out offering carries the same number, but it is not tracked
        # by the pool — reporting it as pool-allocated would claim a reservation
        # that does not exist.
        self.assertIsNone(by_offering["Object storage"]["pool_uuid"])
        self.assertIsNone(by_offering["Object storage"]["pool_scope"])
        self.assertIsNotNone(by_offering["Cluster A"]["pool_uuid"])
        # ...and the two are not merged into one shared row.
        # Sorted because the row order follows the OfferingUser queryset, whose
        # Meta.ordering is ["username", "id"]: the usernames come from a factory
        # sequence, so which of the two rows sorts first depends on the counter
        # the sequence happens to have reached — i.e. on what else ran in the
        # same process. The claim under test is that there are two rows, not
        # which one comes back first.
        self.assertEqual(
            sorted(
                row["offerings"][0]["name"] for row in rows if row["value"] == 100000
            ),
            ["Cluster A", "Object storage"],
        )
