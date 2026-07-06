"""Tests for the POSIX pool backfill data migration helper.

The helper synthesizes one offering-level pool per offering that carries legacy
POSIX configuration, records observed values as identities, and strips the
legacy ``initial_*`` plugin options.
"""

from django.apps import apps as global_apps
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.migrations._posix_pool_backfill import (
    backfill_posix_pools,
)
from waldur_mastermind.marketplace.tests import factories


class PosixPoolBackfillTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={
                "initial_uidnumber": 5000,
                "initial_primarygroup_number": 5000,
                "initial_rolegroup_number": 60000,
                "other_option": "kept",
            },
        )

    def run_backfill(self):
        backfill_posix_pools(global_apps, None)

    def test_creates_offering_pool_from_observed_values(self):
        user = factories.OfferingUserFactory(
            offering=self.offering,
            backend_metadata={"uidnumber": 5005, "primarygroup": 5005},
        )
        self.run_backfill()

        pool = models.PosixIdPool.objects.get(offering=self.offering)
        self.assertEqual(pool.min_uid, 5000)
        self.assertGreaterEqual(pool.max_uid, 5005)
        self.assertEqual(pool.next_uid, 5006)
        # The unified GID range must span up to the historical role-group band.
        self.assertGreaterEqual(pool.max_gid, 60000)

        identity = models.PosixIdentity.objects.get(
            object_id=user.pk,
            content_type__model="offeringuser",
        )
        self.assertEqual(identity.uid, 5005)
        self.assertEqual(identity.gid, 5005)
        self.assertEqual(identity.pool_id, pool.pk)

    def test_strips_legacy_keys_but_keeps_others(self):
        self.run_backfill()
        self.offering.refresh_from_db()
        self.assertNotIn("initial_uidnumber", self.offering.plugin_options)
        self.assertNotIn("initial_rolegroup_number", self.offering.plugin_options)
        self.assertEqual(self.offering.plugin_options["other_option"], "kept")

    def test_pool_created_even_without_observed_values(self):
        # An offering with only legacy options (no users yet) still gets a pool.
        self.run_backfill()
        self.assertTrue(
            models.PosixIdPool.objects.filter(offering=self.offering).exists()
        )

    def test_offering_without_posix_config_is_skipped(self):
        plain = factories.OfferingFactory(customer=self.customer, plugin_options={})
        self.run_backfill()
        self.assertFalse(models.PosixIdPool.objects.filter(offering=plain).exists())

    def test_group_gids_are_recorded(self):
        group = models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={"gid": 6001}
        )
        self.run_backfill()
        identity = models.PosixIdentity.objects.get(
            object_id=group.pk, content_type__model="offeringusergroup"
        )
        self.assertEqual(identity.gid, 6001)
        self.assertIsNone(identity.uid)

    def test_robot_account_identities_are_recorded(self):
        resource = factories.ResourceFactory(offering=self.offering)
        robot = factories.RobotAccountFactory(
            resource=resource,
            backend_metadata={"uidnumber": 5010, "primarygroup": 5011},
        )
        self.run_backfill()
        identity = models.PosixIdentity.objects.get(
            object_id=robot.pk, content_type__model="robotaccount"
        )
        self.assertEqual(identity.uid, 5010)
        self.assertEqual(identity.gid, 5011)

    def test_next_pointers_advance_past_observed_values(self):
        factories.OfferingUserFactory(
            offering=self.offering,
            backend_metadata={"uidnumber": 5008, "primarygroup": 5009},
        )
        self.run_backfill()
        pool = models.PosixIdPool.objects.get(offering=self.offering)
        # next_* is the next free value: max(observed, floor) + 1.
        self.assertEqual(pool.next_uid, 5009)
        self.assertEqual(pool.next_gid, 5010)

    def test_group_gid_colliding_with_user_primary_gid_is_dropped(self):
        # A user's primary GID and a group's GID cannot both be active in one
        # pool (the (pool, gid) partial-unique constraint). Users are collected
        # first, so the user keeps the GID and the colliding group is dropped.
        user = factories.OfferingUserFactory(
            offering=self.offering,
            backend_metadata={"uidnumber": 5005, "primarygroup": 5005},
        )
        group = models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={"gid": 5005}
        )
        self.run_backfill()

        user_identity = models.PosixIdentity.objects.get(
            object_id=user.pk, content_type__model="offeringuser"
        )
        self.assertEqual(user_identity.gid, 5005)
        # The group's only value was the shared GID, so it gets no identity.
        self.assertFalse(
            models.PosixIdentity.objects.filter(
                object_id=group.pk, content_type__model="offeringusergroup"
            ).exists()
        )
