"""API tests for the POSIX ID pool endpoint (marketplace-posix-id-pools)."""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, posix_ids
from waldur_mastermind.marketplace.tests import factories


class PosixIdPoolApiTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

    def create(self, **overrides):
        body = {
            "service_provider": self.service_provider.uuid.hex,
            "min_uid": 1000,
            "max_uid": 1999,
            "min_gid": 2000,
            "max_gid": 2999,
        }
        body.update(overrides)
        self.client.force_authenticate(self.staff)
        return self.client.post(factories.PosixIdPoolFactory.get_list_url(), body)

    def test_create_pool_sets_next_to_min(self):
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        pool = models.PosixIdPool.objects.get(uuid=response.data["uuid"])
        self.assertEqual(pool.next_uid, 1000)
        self.assertEqual(pool.next_gid, 2000)

    def test_create_with_offering_scope(self):
        response = self.create(
            service_provider=None,
            offering=self.offering.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["scope"], "offering")

    def test_two_scopes_rejected(self):
        response = self.create(offering=self.offering.uuid.hex)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_inverted_bounds_rejected(self):
        response = self.create(min_uid=2000, max_uid=1000)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_below_min_id_rejected(self):
        response = self.create(min_uid=500)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_gid_only_pool(self):
        # A pool that manages only GIDs (UIDs sourced externally, e.g. OIDC).
        response = self.create(min_uid=None, max_uid=None)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        pool = models.PosixIdPool.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(pool.min_uid)
        self.assertIsNone(pool.next_uid)
        self.assertEqual(pool.min_gid, 2000)
        self.assertEqual(pool.next_gid, 2000)
        self.assertIsNone(response.data["uid_utilization"])

    def test_create_uid_only_pool(self):
        response = self.create(min_gid=None, max_gid=None)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        pool = models.PosixIdPool.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(pool.min_gid)
        self.assertEqual(pool.min_uid, 1000)

    def test_create_with_no_namespace_rejected(self):
        response = self.create(min_uid=None, max_uid=None, min_gid=None, max_gid=None)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_partial_namespace_rejected(self):
        # min without max (or vice versa) is not a valid namespace definition.
        response = self.create(max_uid=None)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_second_pool_on_same_scope_rejected(self):
        self.assertEqual(self.create().status_code, status.HTTP_201_CREATED)
        response = self.create(min_uid=5000, max_uid=5999, min_gid=6000, max_gid=6999)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_uid_overlap_with_sibling_rejected(self):
        factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=1000,
            max_uid=1999,
            next_uid=1000,
            min_gid=2000,
            max_gid=2999,
            next_gid=2000,
        )
        # Offering pool whose UID range overlaps the provider pool's.
        response = self.create(
            service_provider=None,
            offering=self.offering.uuid.hex,
            min_uid=1500,
            max_uid=2500,
            min_gid=9000,
            max_gid=9999,
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_uid_may_overlap_a_gid_range(self):
        # The two namespaces are independent: a UID range sharing numbers with a
        # GID range is allowed.
        factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=1000,
            max_uid=1999,
            next_uid=1000,
            min_gid=5000,
            max_gid=5999,
            next_gid=5000,
        )
        response = self.create(
            service_provider=None,
            offering=self.offering.uuid.hex,
            min_uid=8000,
            max_uid=8999,
            min_gid=1000,  # overlaps the sibling's UID range, but it is a GID
            max_gid=1999,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_regular_user_cannot_create(self):
        body = {
            "service_provider": self.service_provider.uuid.hex,
            "min_uid": 1000,
            "max_uid": 1999,
            "min_gid": 2000,
            "max_gid": 2999,
        }
        self.client.force_authenticate(self.user)
        response = self.client.post(factories.PosixIdPoolFactory.get_list_url(), body)
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )

    def test_shrink_below_allocated_value_rejected(self):
        pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=1000,
            max_uid=1999,
            next_uid=1010,
            min_gid=2000,
            max_gid=2999,
            next_gid=2000,
        )
        consumer = factories.OfferingUserFactory(offering=self.offering)
        models.PosixIdentity.objects.create(
            pool=pool, uid=1500, consumer=consumer, offering=self.offering
        )
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            factories.PosixIdPoolFactory.get_url(pool), {"max_uid": 1100}
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_stats_reports_per_namespace_utilization(self):
        pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=1000,
            max_uid=1099,
            next_uid=1000,
            min_gid=2000,
            max_gid=2099,
            next_gid=2000,
        )
        consumer = factories.OfferingUserFactory(offering=self.offering)
        models.PosixIdentity.objects.create(
            pool=pool, uid=1000, gid=2000, consumer=consumer, offering=self.offering
        )
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            factories.PosixIdPoolFactory.get_url(pool, action="stats")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["uid"]["used"], 1)
        self.assertEqual(response.data["uid"]["capacity"], 100)
        self.assertEqual(response.data["gid"]["used"], 1)

    def test_destroy_blocked_with_active_identities(self):
        pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=1000,
            max_uid=1999,
            next_uid=1000,
            min_gid=2000,
            max_gid=2999,
            next_gid=2000,
        )
        consumer = factories.OfferingUserFactory(offering=self.offering)
        models.PosixIdentity.objects.create(
            pool=pool, uid=1000, consumer=consumer, offering=self.offering
        )
        self.client.force_authenticate(self.staff)
        response = self.client.delete(factories.PosixIdPoolFactory.get_url(pool))
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def _seed_pools(self, count):
        for _ in range(count):
            offering = factories.OfferingFactory(customer=self.customer)
            factories.PosixIdPoolFactory(
                offering=offering,
                min_uid=10000,
                max_uid=10999,
                next_uid=10000,
                min_gid=20000,
                max_gid=20999,
                next_gid=20000,
            )

    def test_list_query_count_does_not_grow_with_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_authenticate(self.staff)
        self._seed_pools(1)
        with CaptureQueriesContext(connection) as few:
            self.client.get(factories.PosixIdPoolFactory.get_list_url())
        self._seed_pools(4)
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(factories.PosixIdPoolFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Per-namespace utilization is annotated, so adding rows must not add
        # per-row stats queries.
        self.assertLessEqual(len(many.captured_queries) - len(few.captured_queries), 1)


class PosixIdPoolResolutionTest(test.APITestCase):
    def test_resolve_returns_none_without_pool(self):
        offering = factories.OfferingFactory()
        self.assertIsNone(posix_ids.resolve(offering))
