"""Unit tests for the POSIX ID pool allocator (marketplace.posix_ids).

Covers pool resolution, sequential high-water-mark allocation, auto-recycle of
released values, idempotency, exhaustion (409), the DB-unique collision
backstop, and release-on-delete.
"""

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from rest_framework import test

from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, posix_ids
from waldur_mastermind.marketplace.tests import factories


class PosixIdAllocatorTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=1000,
            max_uid=1004,
            next_uid=1000,
            min_gid=2000,
            max_gid=2004,
            next_gid=2000,
        )

    def consumer(self):
        return factories.OfferingUserFactory(offering=self.offering)

    def active_identity(self, consumer):
        ct = ContentType.objects.get_for_model(consumer.__class__)
        return models.PosixIdentity.objects.filter(
            content_type=ct, object_id=consumer.pk, released_at__isnull=True
        ).first()

    def test_resolve_prefers_offering_pool_over_provider(self):
        offering_pool = factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=5000,
            max_uid=5999,
            next_uid=5000,
            min_gid=6000,
            max_gid=6999,
            next_gid=6000,
        )
        self.assertEqual(posix_ids.resolve(self.offering).pk, offering_pool.pk)

    def test_resolve_falls_back_to_provider_pool(self):
        self.assertEqual(posix_ids.resolve(self.offering).pk, self.pool.pk)

    def test_no_pool_returns_none(self):
        self.pool.delete()
        self.assertIsNone(
            posix_ids.allocate(self.offering, posix_ids.UID, self.consumer())
        )

    def test_sequential_allocation_advances_high_water_mark(self):
        values = [
            posix_ids.allocate(self.offering, posix_ids.UID, self.consumer())
            for _ in range(3)
        ]
        self.assertEqual(values, [1000, 1001, 1002])
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.next_uid, 1003)

    def test_uid_and_gid_are_independent(self):
        consumer = self.consumer()
        uid = posix_ids.allocate(self.offering, posix_ids.UID, consumer)
        gid = posix_ids.allocate(self.offering, posix_ids.GID, consumer)
        self.assertEqual(uid, 1000)
        self.assertEqual(gid, 2000)
        identity = self.active_identity(consumer)
        self.assertEqual((identity.uid, identity.gid), (1000, 2000))

    def test_allocation_is_idempotent(self):
        consumer = self.consumer()
        first = posix_ids.allocate(self.offering, posix_ids.UID, consumer)
        second = posix_ids.allocate(self.offering, posix_ids.UID, consumer)
        self.assertEqual(first, second)
        self.assertEqual(models.PosixIdentity.objects.filter(uid=first).count(), 1)

    def test_exhaustion_raises_409(self):
        for _ in range(5):  # fills 1000..1004
            posix_ids.allocate(self.offering, posix_ids.UID, self.consumer())
        with self.assertRaises(IncorrectStateException):
            posix_ids.allocate(self.offering, posix_ids.UID, self.consumer())

    def test_released_value_is_recycled(self):
        first = self.consumer()
        value = posix_ids.allocate(self.offering, posix_ids.UID, first)
        second = self.consumer()
        posix_ids.allocate(self.offering, posix_ids.UID, second)

        posix_ids.release_posix_allocations(first)
        # The lowest released, in-bounds, not-active value is handed back first.
        third = self.consumer()
        recycled = posix_ids.allocate(self.offering, posix_ids.UID, third)
        self.assertEqual(recycled, value)

    def test_high_water_mark_skips_value_held_by_override(self):
        # Pin 1000 to one consumer, then auto-allocate: the counter steps over it.
        pinned = self.consumer()
        models.PosixIdentity.objects.create(
            pool=self.pool, uid=1000, consumer=pinned, offering=self.offering
        )
        value = posix_ids.allocate(self.offering, posix_ids.UID, self.consumer())
        self.assertEqual(value, 1001)

    def test_db_unique_constraint_blocks_duplicate_active_uid(self):
        first = self.consumer()
        models.PosixIdentity.objects.create(
            pool=self.pool, uid=1000, consumer=first, offering=self.offering
        )
        second = self.consumer()
        with self.assertRaises(IntegrityError):
            models.PosixIdentity.objects.create(
                pool=self.pool, uid=1000, consumer=second, offering=self.offering
            )

    def test_release_marks_identity_released(self):
        consumer = self.consumer()
        posix_ids.allocate(self.offering, posix_ids.UID, consumer)
        self.assertIsNotNone(self.active_identity(consumer))
        posix_ids.release_posix_allocations(consumer)
        self.assertIsNone(self.active_identity(consumer))

    def test_existing_identity_stays_in_its_pool_when_override_added(self):
        # Allocate a UID from the provider pool, then add an offering override
        # pool. The later GID allocation must stay in the consumer's original
        # pool, so both namespaces share one partition for the unique constraint.
        consumer = self.consumer()
        posix_ids.allocate(self.offering, posix_ids.UID, consumer)
        factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=5000,
            max_uid=5999,
            next_uid=5000,
            min_gid=6000,
            max_gid=6999,
            next_gid=6000,
        )
        gid = posix_ids.allocate(self.offering, posix_ids.GID, consumer)
        identity = self.active_identity(consumer)
        self.assertEqual(identity.pool_id, self.pool.pk)
        self.assertEqual(gid, 2000)  # the provider pool's GID sequence, not 6000

    def test_gid_only_pool_skips_uid_allocation(self):
        # An offering-override pool that manages only GIDs (UIDs come from an
        # external source such as OIDC): allocate(UID) is a no-op, GID works.
        factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=6000,
            max_gid=6999,
            next_gid=6000,
        )
        consumer = self.consumer()
        uid = posix_ids.allocate(self.offering, posix_ids.UID, consumer)
        gid = posix_ids.allocate(self.offering, posix_ids.GID, consumer)
        self.assertIsNone(uid)
        self.assertEqual(gid, 6000)
        identity = self.active_identity(consumer)
        self.assertIsNone(identity.uid)
        self.assertEqual(identity.gid, 6000)

    def test_override_on_unmanaged_namespace_is_rejected(self):
        from django.core.exceptions import ValidationError

        factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=6000,
            max_gid=6999,
            next_gid=6000,
        )
        consumer = self.consumer()
        with self.assertRaises(ValidationError):
            posix_ids.set_value(consumer, posix_ids.UID, 6000, self.offering)
