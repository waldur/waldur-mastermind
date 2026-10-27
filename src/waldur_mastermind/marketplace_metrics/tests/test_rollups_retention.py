import datetime
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.configuration.testing import override_config
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums, models, retention, rollups
from waldur_mastermind.marketplace_metrics.handlers import projects_with_metrics

from . import factories

NOW = datetime.datetime(2026, 10, 15, 12, 30, tzinfo=datetime.UTC)


def at(**ago):
    return NOW - datetime.timedelta(**ago)


@freeze_time(NOW)
class RollupTest(TestCase):
    def setUp(self):
        self.gauge = factories.MetricSeriesFactory(
            offering_metric=factories.OfferingMetricFactory(
                definition=factories.MetricDefinitionFactory(
                    kind=enums.MetricKinds.GAUGE
                )
            )
        )
        self.counter = factories.MetricSeriesFactory()

    def add(self, series, value, **ago):
        models.MetricPoint.objects.create(
            series=series, timestamp=at(**ago), value=value
        )

    def rollup(self, series, granularity, bucket):
        return models.MetricRollup.objects.get(
            series=series, granularity=granularity, bucket_start=bucket
        )

    def test_hourly_and_daily_buckets_summarise_points(self):
        self.add(self.gauge, 4, minutes=25)  # 12:05
        self.add(self.gauge, 8, minutes=15)  # 12:15
        self.add(self.gauge, 2, hours=3)  # 09:30

        rollups.roll_up()

        hour = self.rollup(
            self.gauge,
            enums.Granularities.HOUR,
            NOW.replace(minute=0),
        )
        self.assertEqual(
            (hour.count, hour.sum, hour.min, hour.max, hour.last),
            (2, Decimal(12), Decimal(4), Decimal(8), Decimal(8)),
        )
        day = self.rollup(
            self.gauge, enums.Granularities.DAY, NOW.replace(hour=0, minute=0)
        )
        self.assertEqual((day.count, day.sum, day.last), (3, Decimal(14), Decimal(8)))

    def later(self, minutes):
        return freeze_time(NOW + datetime.timedelta(minutes=minutes))

    def test_a_late_correction_inside_the_window_is_picked_up(self):
        self.add(self.counter, 5, days=3)
        rollups.roll_up()
        with self.later(30):
            rollups.roll_up()

        with self.later(60):
            # As ingest corrects a point: received anew.
            models.MetricPoint.objects.filter(series=self.counter).update(
                value=7, received_at=timezone.now()
            )
            rollups.roll_up()

        day = models.MetricRollup.objects.get(
            series=self.counter, granularity=enums.Granularities.DAY
        )
        self.assertEqual(day.sum, Decimal(7))

    def test_buckets_before_the_window_are_left_alone(self):
        self.add(self.counter, 5, days=20)
        rollups.roll_up(since=at(days=30))
        models.MetricPoint.objects.filter(series=self.counter).update(
            value=7, received_at=NOW
        )

        rollups.roll_up()
        rollups.roll_up(whole_window=True)

        day = models.MetricRollup.objects.get(
            series=self.counter, granularity=enums.Granularities.DAY
        )
        self.assertEqual(day.sum, Decimal(5))

    def test_a_run_writes_only_what_changed(self):
        self.add(self.gauge, 4, minutes=25)
        self.add(self.counter, 5, days=3)
        # An hourly and a daily bucket of each series.
        self.assertEqual(rollups.roll_up(), 4)

        # Within the overlap the points are looked at again, and unchanged.
        with self.later(30):
            self.assertEqual(rollups.roll_up(), 0)
            self.assertEqual(rollups.roll_up(whole_window=True), 0)
        # Past it, nothing is new.
        with self.later(60):
            self.assertEqual(rollups.roll_up(), 0)

    def test_only_buckets_with_newly_received_points_are_recomputed(self):
        self.add(self.gauge, 4, hours=2)
        self.add(self.counter, 5, days=3)
        rollups.roll_up()
        # The next run looks once more at what the last one may have missed.
        with self.later(30):
            rollups.roll_up()
        # Wrong, to show which buckets a run recomputes.
        models.MetricRollup.objects.filter(series=self.gauge).update(sum=999)
        later = NOW + datetime.timedelta(hours=1)

        with freeze_time(later):
            # A late point, received now, for a bucket three days old.
            models.MetricPoint.objects.create(
                series=self.counter, timestamp=at(days=3, minutes=5), value=2
            )
            written = rollups.roll_up()

        self.assertEqual(written, 2)
        day = self.rollup(
            self.counter,
            enums.Granularities.DAY,
            at(days=3).replace(hour=0, minute=0),
        )
        self.assertEqual(day.sum, Decimal(7))
        gauge_sums = models.MetricRollup.objects.filter(series=self.gauge)
        self.assertEqual(set(gauge_sums.values_list("sum", flat=True)), {999})

        # The daily run recomputes the whole window, and puts it right.
        with freeze_time(later):
            rollups.roll_up(whole_window=True)
        self.assertEqual(set(gauge_sums.values_list("sum", flat=True)), {4})

    def test_a_point_committed_after_a_run_it_was_received_before_is_rolled_up(
        self,
    ):
        rollups.roll_up()

        with self.later(15):
            # Received before the run started, committed after it: within
            # the overlap it is rolled up; before it, left to the daily run.
            for series, received in ((self.counter, 14), (self.gauge, 16)):
                models.MetricPoint.objects.create(
                    series=series,
                    timestamp=at(minutes=5),
                    value=3,
                    received_at=at(minutes=received),
                )
            rollups.roll_up()

        hour = self.rollup(
            self.counter, enums.Granularities.HOUR, NOW.replace(minute=0)
        )
        self.assertEqual(hour.sum, Decimal(3))
        self.assertFalse(models.MetricRollup.objects.filter(series=self.gauge))

    def test_a_run_from_inside_the_window_does_not_move_the_next_start(self):
        rollups.roll_up(since=at(hours=1))

        self.assertIsNone(models.RollupState.objects.get().rolled_up_until)

    def test_a_sum_is_clamped_to_what_its_column_holds(self):
        self.add(self.counter, 6, minutes=10)
        self.add(self.counter, 7, minutes=20)

        with mock.patch.object(rollups, "MAX_SUM", Decimal(10)):
            rollups.roll_up()

        hour = self.rollup(
            self.counter, enums.Granularities.HOUR, NOW.replace(minute=0)
        )
        self.assertEqual(hour.sum, Decimal(10))

    def test_a_point_stored_without_received_at_gets_it_from_the_database(self):
        # As code from before the column, during a rolling deploy.
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {models.MetricPoint._meta.db_table} "
                "(series_id, timestamp, value) VALUES (%s, %s, %s)",
                [self.counter.id, at(minutes=5), 1],
            )

        self.assertIsNotNone(models.MetricPoint.objects.get().received_at)

    def test_without_a_previous_run_the_whole_window_is_recomputed(self):
        self.add(self.counter, 5, days=3)
        rollups.roll_up()
        models.MetricRollup.objects.filter(series=self.counter).update(sum=999)
        models.RollupState.objects.all().delete()

        with freeze_time(NOW + datetime.timedelta(hours=1)):
            rollups.roll_up()

        sums = models.MetricRollup.objects.filter(series=self.counter)
        self.assertEqual(set(sums.values_list("sum", flat=True)), {5})


@freeze_time(NOW)
class RetentionTest(TestCase):
    def setUp(self):
        self.policy = models.RetentionPolicy.objects.create(
            name="short", raw_days=10, hourly_days=20, daily_days=40
        )
        definition = factories.MetricDefinitionFactory(retention_policy=self.policy)
        self.series = factories.MetricSeriesFactory(
            offering_metric=factories.OfferingMetricFactory(definition=definition)
        )
        self.default_series = factories.MetricSeriesFactory()

    def add(self, series, **ago):
        models.MetricPoint.objects.create(series=series, timestamp=at(**ago), value=1)

    def add_rollup(self, series, granularity, **ago):
        models.MetricRollup.objects.create(
            series=series,
            granularity=granularity,
            bucket_start=at(**ago),
            count=1,
            sum=1,
            min=1,
            max=1,
            last=1,
            last_timestamp=at(**ago),
        )

    def test_each_tier_expires_on_its_own_schedule(self):
        for days in (5, 15):
            self.add(self.series, days=days)
        for days in (15, 25):
            self.add_rollup(self.series, enums.Granularities.HOUR, days=days)
        for days in (30, 50):
            self.add_rollup(self.series, enums.Granularities.DAY, days=days)

        retention.enforce()

        self.assertEqual(
            models.MetricPoint.objects.filter(series=self.series).count(), 1
        )
        remaining = models.MetricRollup.objects.filter(series=self.series)
        self.assertEqual(
            sorted(remaining.values_list("granularity", flat=True)),
            [enums.Granularities.DAY, enums.Granularities.HOUR],
        )

    def test_definitions_without_a_policy_follow_the_default(self):
        self.add(self.default_series, days=60)
        self.add(self.default_series, days=100)

        retention.enforce()

        # "standard" keeps raw points for 90 days.
        self.assertEqual(
            models.MetricPoint.objects.filter(series=self.default_series).count(), 1
        )

    def test_a_series_with_nothing_left_is_dropped(self):
        models.MetricSeries.objects.filter(pk=self.series.pk).update(created=at(days=5))
        self.add(self.series, days=15)

        retention.enforce()

        self.assertFalse(models.MetricSeries.objects.filter(pk=self.series.pk).exists())

    def test_archived_metrics_are_purged_after_the_grace_period(self):
        offering_metric = self.series.offering_metric
        self.add(self.series, days=1)
        models.OfferingMetric.objects.filter(pk=offering_metric.pk).update(
            state=enums.OfferingMetricStates.ARCHIVED, modified=at(days=31)
        )

        retention.purge_archived()

        self.assertFalse(
            models.OfferingMetric.objects.filter(pk=offering_metric.pk).exists()
        )
        self.assertFalse(
            models.MetricPoint.objects.filter(series__pk=self.series.pk).exists()
        )


class HasMetricsTest(TestCase):
    def test_only_running_resources_of_offerings_reporting_a_metric_count(self):
        offering_metric = factories.OfferingMetricFactory()
        running = marketplace_factories.ResourceFactory(
            offering=offering_metric.offering, state=ResourceStates.OK
        )
        # A terminated sibling must not hide the running one.
        marketplace_factories.ResourceFactory(
            offering=offering_metric.offering, state=ResourceStates.TERMINATED
        )
        terminated = marketplace_factories.ResourceFactory(
            offering=factories.OfferingMetricFactory().offering,
            state=ResourceStates.TERMINATED,
        )
        plain = marketplace_factories.ResourceFactory(state=ResourceStates.OK)

        found = projects_with_metrics(
            [running.project_id, terminated.project_id, plain.project_id]
        )

        self.assertEqual(found, {running.project_id})

    def test_an_archived_metric_does_not_count(self):
        offering_metric = factories.OfferingMetricFactory(
            state=enums.OfferingMetricStates.ARCHIVED
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering_metric.offering, state=ResourceStates.OK
        )

        self.assertEqual(projects_with_metrics([resource.project_id]), set())


@freeze_time(NOW)
class ReviewFixesTest(TestCase):
    def test_a_resumed_metric_is_not_purged(self):
        """Purge was requested while archived; resume came first."""
        series = factories.MetricSeriesFactory()
        offering_metric = series.offering_metric
        models.MetricPoint.objects.create(series=series, timestamp=at(hours=1), value=1)

        self.assertFalse(retention.purge(offering_metric.id))

        self.assertTrue(models.MetricPoint.objects.filter(series=series).exists())

    def test_retention_still_trims_when_the_default_policy_is_gone(self):
        series = factories.MetricSeriesFactory()
        models.MetricPoint.objects.create(
            series=series, timestamp=at(days=100), value=1
        )
        models.RetentionPolicy.objects.filter(name="standard").delete()

        retention.enforce()

        # The built-in defaults keep raw points for 90 days.
        self.assertFalse(models.MetricPoint.objects.filter(series=series).exists())

    def test_rollups_never_recompute_days_retention_has_started_to_trim(self):
        models.RetentionPolicy.objects.create(name="short", raw_days=3, hourly_days=10)

        with override_config(METRICS_LATE_DATA_DAYS=7):
            since = rollups.recompute_since()

        # raw points go back 3 days; the first whole day left starts 2 days ago.
        self.assertEqual(
            since, NOW.replace(hour=0, minute=0) - datetime.timedelta(days=2)
        )
