"""Coverage for 0281_rerun_data_migrations_skipped_by_squashes.

A replaces-squash that carried no data operations was applied, in place of its
originals, on every database upgrading from before its range. The migration must
tell that case apart from a database that applied the originals - by the
``applied`` timestamps of the replaced rows in ``django_migrations`` - and re-run
the skipped backfills only then.
"""

from datetime import timedelta
from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase
from django.utils import timezone

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories

PACKAGE = "waldur_mastermind.marketplace.migrations"
repair = import_module(f"{PACKAGE}.0281_rerun_data_migrations_skipped_by_squashes")
SQUASH_0226 = ("marketplace", "0226_squashed_0263")
SQUASH_0264 = ("marketplace", "0264_squashed_0279")
INSTALLED = ("contenttypes", "0001_initial")


def _recorded(squash, names, spread=timedelta(0), installed_days_ago=30):
    """Record ``names`` then ``squash`` as one block, applied over ``spread``.

    ``spread`` 0 is the block a replacement squash leaves; the database's oldest
    row is pushed ``installed_days_ago`` back so the block reads as an upgrade.
    """
    # CI builds the test database without migrations, so the recorder table may
    # not exist yet; the migration itself only ever runs where it does.
    MigrationRecorder(connection).ensure_schema()
    rows = MigrationRecorder.Migration.objects
    rows.filter(app__in={app for app, _ in names} | {squash[0]}).filter(
        name__in={name for _, name in names} | {squash[1]}
    ).delete()
    for key in [*names, squash]:
        rows.create(app=key[0], name=key[1])
    now = timezone.now()
    for i, key in enumerate(names):
        rows.filter(app=key[0], name=key[1]).update(
            applied=now + spread * i / max(len(names) - 1, 1)
        )
    rows.filter(app=squash[0], name=squash[1]).update(applied=now + spread)
    rows.get_or_create(app=INSTALLED[0], name=INSTALLED[1])
    rows.filter(app=INSTALLED[0], name=INSTALLED[1]).update(
        applied=now - timedelta(days=installed_days_ago)
    )


def _run_repair():
    with connection.schema_editor(atomic=False) as schema_editor:
        repair.rerun_skipped_data_migrations(django_apps, schema_editor)


class AppliedAsReplacementTest(TestCase):
    SQUASH = ("t", "0001_squashed_0003")
    NAMES = [("t", "0001_a"), ("t", "0002_b"), ("t", "0003_c")]

    def _applied(self):
        return repair.applied_as_replacement(connection, self.SQUASH, self.NAMES)

    def test_one_block_recorded_in_one_instant_on_an_upgrade_is_the_squash(self):
        _recorded(self.SQUASH, self.NAMES, spread=timedelta(milliseconds=30))
        self.assertTrue(self._applied())

    def test_rows_spread_over_seconds_mean_the_originals_were_applied(self):
        _recorded(self.SQUASH, self.NAMES, spread=timedelta(seconds=5))
        self.assertFalse(self._applied())

    def test_a_squash_row_recorded_apart_from_the_block_is_check_replacements(
        self,
    ):
        _recorded(self.SQUASH, self.NAMES)
        rows = MigrationRecorder.Migration.objects
        rows.filter(app="t", name=self.SQUASH[1]).delete()
        rows.create(app="other", name="0001_between")
        rows.create(app="t", name=self.SQUASH[1])
        self.assertFalse(self._applied())

    def test_a_block_recorded_at_install_time_is_a_fresh_install(self):
        _recorded(self.SQUASH, self.NAMES, installed_days_ago=0)
        self.assertFalse(self._applied())

    def test_an_unapplied_original_means_the_squash_is_not_in_use(self):
        _recorded(self.SQUASH, self.NAMES[:-1])
        self.assertFalse(self._applied())


class RerunSkippedDataMigrationsTest(TestCase):
    def setUp(self):
        self.stale_ct, _ = ContentType.objects.get_or_create(
            app_label="support", model="offering"
        )
        self.slurm_ct, _ = ContentType.objects.get_or_create(
            app_label="waldur_slurm", model="allocation"
        )
        self.resource = factories.ResourceFactory()
        self.slurm_resource = factories.ResourceFactory()
        models.Resource.objects.filter(pk=self.resource.pk).update(
            content_type=self.stale_ct, object_id=1
        )
        models.Resource.objects.filter(pk=self.slurm_resource.pk).update(
            content_type=self.slurm_ct, object_id=2
        )

    def test_backfills_run_again_when_the_squashes_were_applied(self):
        _recorded(SQUASH_0226, repair.MARKETPLACE_0226)
        _recorded(SQUASH_0264, repair.MARKETPLACE_0264)

        _run_repair()

        self.resource.refresh_from_db()
        self.slurm_resource.refresh_from_db()
        self.assertIsNone(self.resource.content_type)
        self.assertIsNone(self.slurm_resource.content_type)
        self.assertFalse(
            ContentType.objects.filter(app_label="waldur_slurm").exists(),
            "0265 removes the content types of the deleted SLURM app",
        )

    def test_nothing_changes_when_the_originals_were_applied(self):
        _recorded(SQUASH_0226, repair.MARKETPLACE_0226, spread=timedelta(minutes=3))
        _recorded(SQUASH_0264, repair.MARKETPLACE_0264, spread=timedelta(minutes=3))

        _run_repair()

        self.resource.refresh_from_db()
        self.slurm_resource.refresh_from_db()
        self.assertEqual(self.resource.content_type, self.stale_ct)
        self.assertEqual(self.slurm_resource.content_type, self.slurm_ct)
        self.assertTrue(ContentType.objects.filter(app_label="waldur_slurm").exists())
