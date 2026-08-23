"""Tests for the offering-user POSIX allocations / identities read actions.

Surfaces, per offering user, the UID / primary GID and the POSIX ID pool each
value is tracked by (or that it is tracked by none).
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.tests import factories


class OfferingUserPosixAllocationsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer, name="HPC Cluster"
        )
        factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=9000,
            max_uid=9099,
            next_uid=9000,
            min_gid=9500,
            max_gid=9599,
            next_gid=9500,
        )
        self.offering_user = factories.OfferingUserFactory(offering=self.offering)
        utils.setup_linux_related_data(self.offering_user, self.offering)
        self.offering_user.save()

    def get_url(self, offering_user=None):
        return factories.OfferingUserFactory.get_url(
            offering_user or self.offering_user, action="posix-allocations"
        )

    def test_shared_offerings_are_listed(self):
        second_offering = factories.OfferingFactory(
            customer=self.customer, name="HPC Cluster 2"
        )
        sibling = factories.OfferingUserFactory(
            offering=second_offering, user=self.offering_user.user
        )
        utils.setup_linux_related_data(sibling, second_offering)
        sibling.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # The offering has its own pool, so nothing is shared with the sibling.
        for row in response.data:
            self.assertEqual(row["shared_with_offerings"], [])

    def test_lists_uid_and_gid_with_originating_pool(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        by_namespace = {row["namespace"]: row for row in response.data}

        uid = by_namespace["uid"]
        self.assertEqual(uid["value"], 9000)
        self.assertEqual(uid["scope"], "offering")
        self.assertEqual(uid["scope_name"], "HPC Cluster")
        self.assertIsNotNone(uid["pool_uuid"])

        gid = by_namespace["gid"]
        self.assertEqual(gid["value"], 9500)
        self.assertIsNotNone(gid["pool_uuid"])

    def test_value_without_identity_reports_no_pool(self):
        offering_user = factories.OfferingUserFactory(
            offering=self.offering, backend_metadata={"uidnumber": 4242}
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(offering_user))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = {row["namespace"]: row for row in response.data}
        self.assertEqual(rows["uid"]["value"], 4242)
        self.assertIsNone(rows["uid"]["pool_uuid"])
        self.assertIsNone(rows["uid"]["scope"])
        self.assertNotIn("gid", rows)

    def test_empty_metadata_yields_no_rows(self):
        offering_user = factories.OfferingUserFactory(
            offering=self.offering, backend_metadata={}
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(offering_user))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, [])


class UserPosixIdentitiesTest(test.APITestCase):
    """The consolidated cross-offering POSIX identities action."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        # A provider-level pool applies to every offering of the provider.
        factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=199999,
            next_uid=100000,
            min_gid=200000,
            max_gid=299999,
            next_gid=200000,
        )
        self.offering_a = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.offering_b = factories.OfferingFactory(
            customer=self.customer, name="Cluster B"
        )
        self.user = structure_factories.UserFactory()
        for offering in (self.offering_a, self.offering_b):
            offering_user = factories.OfferingUserFactory(
                offering=offering, user=self.user
            )
            utils.setup_linux_related_data(offering_user, offering)
            offering_user.save()

    def get_url(self):
        return factories.OfferingUserFactory.get_list_url() + "posix_identities/"

    def test_consolidates_identities_across_offerings(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(), {"user_uuid": self.user.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # One shared UID and one shared primary GID, not one pair per offering.
        by_namespace = {row["namespace"]: row for row in response.data}
        self.assertEqual(len(response.data), 2)
        self.assertEqual(set(by_namespace), {"uid", "gid"})
        for row in response.data:
            self.assertEqual(
                {offering["name"] for offering in row["offerings"]},
                {"Cluster A", "Cluster B"},
            )
            self.assertTrue(row["pool_uuid"])
            self.assertEqual(row["pool_scope"], "service_provider")

    def test_deprecated_offering_fields_mirror_the_first_sharing_offering(self):
        # Kept for one cycle so clients written against the previous
        # one-row-per-offering shape keep rendering a name.
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(), {"user_uuid": self.user.uuid.hex})
        for row in response.data:
            self.assertEqual(row["offering_name"], row["offerings"][0]["name"])
            self.assertEqual(row["offering_uuid"], row["offerings"][0]["uuid"])

    def test_offering_with_its_own_pool_is_reported_separately(self):
        factories.PosixIdPoolFactory(
            offering=self.offering_b,
            min_uid=500000,
            max_uid=599999,
            next_uid=500000,
            min_gid=600000,
            max_gid=699999,
            next_gid=600000,
        )
        offering_user = models.OfferingUser.objects.get(
            offering=self.offering_b, user=self.user
        )
        offering_user.backend_metadata = {}
        utils.setup_linux_related_data(offering_user, self.offering_b)
        offering_user.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(), {"user_uuid": self.user.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        uid_rows = [row for row in response.data if row["namespace"] == "uid"]
        self.assertEqual(len(uid_rows), 2)
        by_value = {row["value"]: row for row in uid_rows}
        self.assertEqual(
            [offering["name"] for offering in by_value[100000]["offerings"]],
            ["Cluster A"],
        )
        self.assertEqual(
            [offering["name"] for offering in by_value[500000]["offerings"]],
            ["Cluster B"],
        )

    def test_user_uuid_is_required(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
