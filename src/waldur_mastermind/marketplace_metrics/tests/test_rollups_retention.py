import datetime
from decimal import Decimal

from constance.test import override_config
from django.test import TestCase
from freezegun import freeze_time

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

    def test_a_late_correction_inside_the_window_is_picked_up(self):
        self.add(self.counter, 5, days=3)
        rollups.roll_up()
        models.MetricPoint.objects.filter(series=self.counter).update(value=7)

        rollups.roll_up()

        day = models.MetricRollup.objects.get(
            series=self.counter, granularity=enums.Granularities.DAY
        )
        self.assertEqual(day.sum, Decimal(7))

    def test_buckets_before_the_window_are_left_alone(self):
        self.add(self.counter, 5, days=20)
        rollups.roll_up(since=at(days=30))
        models.MetricPoint.objects.filter(series=self.counter).update(value=7)

        rollups.roll_up()

        day = models.MetricRollup.objects.get(
            series=self.counter, granularity=enums.Granularities.DAY
        )
        self.assertEqual(day.sum, Decimal(5))


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
