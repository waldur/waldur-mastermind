"""Tests for the offering-user POSIX allocations / identities read actions.

Surfaces, per offering user, the UID / primary GID and the POSIX ID pool each
value is tracked by (or that it is tracked by none).
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import utils
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

        offerings = {row["offering_name"] for row in response.data}
        self.assertEqual(offerings, {"Cluster A", "Cluster B"})
        for name in ("Cluster A", "Cluster B"):
            namespaces = {
                r["namespace"] for r in response.data if r["offering_name"] == name
            }
            self.assertEqual(namespaces, {"uid", "gid"})
        self.assertTrue(all(row["pool_uuid"] for row in response.data))

    def test_user_uuid_is_required(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
