"""Retrofit of per-offering POSIX identities onto one identity per user.

Covers the data migration helper, which only moves rows that can move safely,
and the ``collapse_posix_identities`` command, which is what actually rewrites a
live account's UID — reported first, applied only on ``--apply``.
"""

from io import StringIO

from django.apps import apps as global_apps
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, posix_ids
from waldur_mastermind.marketplace.migrations._posix_identity_principals import (
    backfill_posix_identity_principals,
)
from waldur_mastermind.marketplace.tests import factories


class LegacyIdentityFixture(test.APITestCase):
    """Two accounts of one user in one pool, each with its own identity."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100002,
            min_gid=200000,
            max_gid=200099,
            next_gid=200002,
        )
        self.offering_a = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.offering_b = factories.OfferingFactory(
            customer=self.customer, name="Cluster B"
        )
        self.user = structure_factories.UserFactory()
        self.offering_user_ct = ContentType.objects.get_for_model(models.OfferingUser)
        self.account_a = self.legacy_account(self.offering_a, 100000, 200000)
        self.account_b = self.legacy_account(self.offering_b, 100001, 200001)

    def legacy_account(self, offering, uid, gid):
        offering_user = factories.OfferingUserFactory(
            offering=offering,
            user=self.user,
            backend_metadata={"uidnumber": uid, "primarygroup": gid},
        )
        models.PosixIdentity.objects.create(
            pool=self.pool,
            offering=offering,
            content_type=self.offering_user_ct,
            object_id=offering_user.id,
            uid=uid,
            gid=gid,
        )
        return offering_user


class PosixIdentityBackfillTest(LegacyIdentityFixture):
    def test_oldest_identity_of_a_group_becomes_user_scoped(self):
        backfill_posix_identity_principals(global_apps, None)

        canonical = models.PosixIdentity.objects.get(uid=100000)
        self.assertEqual(canonical.user_id, self.user.id)
        self.assertIsNone(canonical.content_type_id)
        self.assertIsNone(canonical.object_id)

    def test_duplicate_is_left_untouched_for_the_operator(self):
        backfill_posix_identity_principals(global_apps, None)

        duplicate = models.PosixIdentity.objects.get(uid=100001)
        self.assertIsNone(duplicate.user_id)
        self.assertEqual(duplicate.content_type_id, self.offering_user_ct.id)
        self.assertIsNone(duplicate.released_at)
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.backend_metadata["uidnumber"], 100001)

    def test_robot_account_identity_is_not_touched(self):
        resource = factories.ResourceFactory(offering=self.offering_a)
        robot_account = factories.RobotAccountFactory(resource=resource)
        identity = models.PosixIdentity.objects.create(
            pool=self.pool,
            offering=self.offering_a,
            content_type=ContentType.objects.get_for_model(models.RobotAccount),
            object_id=robot_account.id,
            uid=100050,
        )
        backfill_posix_identity_principals(global_apps, None)

        identity.refresh_from_db()
        self.assertIsNone(identity.user_id)

    def test_deleting_an_account_releases_its_legacy_identity(self):
        # Until the collapse command runs, the duplicate rows stay consumer
        # scoped. Deleting such an account must still free its value instead of
        # reserving it forever.
        backfill_posix_identity_principals(global_apps, None)
        self.account_b.delete()

        duplicate = models.PosixIdentity.objects.get(uid=100001)
        self.assertIsNotNone(duplicate.released_at)
        canonical = models.PosixIdentity.objects.get(uid=100000)
        self.assertIsNone(canonical.released_at)


class CollapsePosixIdentitiesCommandTest(LegacyIdentityFixture):
    def setUp(self):
        super().setUp()
        backfill_posix_identity_principals(global_apps, None)

    def run_command(self, **options):
        out = StringIO()
        call_command("collapse_posix_identities", stdout=out, **options)
        return out.getvalue()

    def test_dry_run_reports_the_map_without_writing(self):
        output = self.run_command()

        self.assertIn("Cluster B: uid 100001 -> 100000", output)
        self.assertIn("chown required", output)
        self.assertIn("Re-run with --apply", output)
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.backend_metadata["uidnumber"], 100001)
        self.assertEqual(
            models.PosixIdentity.objects.filter(released_at__isnull=True).count(), 2
        )

    def test_dry_run_lists_values_that_will_not_be_recycled(self):
        output = self.run_command()
        self.assertIn("NOT recycled", output)
        self.assertIn("uid: 100001", output)
        self.assertIn("gid: 200001", output)

    def test_apply_collapses_onto_the_canonical_identity(self):
        self.run_command(apply=True)

        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.backend_metadata["uidnumber"], 100000)
        self.assertEqual(self.account_b.backend_metadata["primarygroup"], 200000)
        self.assertEqual(self.account_a.backend_metadata["uidnumber"], 100000)

        active = models.PosixIdentity.objects.filter(released_at__isnull=True)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().user_id, self.user.id)

    def test_freed_value_is_withheld_from_recycling(self):
        self.run_command(apply=True)

        superseded = models.PosixIdentity.objects.get(uid=100001)
        self.assertIsNotNone(superseded.released_at)
        self.assertFalse(superseded.recyclable)

        # The next allocation must not hand 100001 to somebody else.
        newcomer = factories.OfferingUserFactory(
            offering=self.offering_a, user=structure_factories.UserFactory()
        )
        self.assertEqual(
            posix_ids.allocate(self.offering_a, posix_ids.UID, newcomer), 100002
        )

    def test_pinned_identity_wins_over_the_oldest(self):
        # 100090 sits above the pool's high-water mark, so it can only have been
        # pinned by hand: the collapse keeps it and moves the allocated one.
        models.PosixIdentity.objects.filter(uid=100001).update(uid=100090)
        self.account_b.backend_metadata["uidnumber"] = 100090
        self.account_b.save()

        self.run_command(apply=True)

        self.account_a.refresh_from_db()
        self.assertEqual(self.account_a.backend_metadata["uidnumber"], 100090)
        self.assertEqual(
            models.PosixIdentity.objects.get(released_at__isnull=True).uid, 100090
        )

    def test_nothing_to_collapse_is_reported(self):
        self.run_command(apply=True)
        output = self.run_command()
        self.assertIn("Nothing to collapse", output)
