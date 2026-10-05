import datetime
from unittest import mock

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums, models, query, rollups
from waldur_mastermind.marketplace_metrics.ingest import attributes_hash

from . import factories

SERIES = "/api/marketplace-metric-series/"
PROJECT_METRICS = "/api/marketplace-project-metrics/"
GOALS = "/api/marketplace-metric-goals/"


class Scenario(test.APITestCase):
    """A provider's offering reports two metrics for two resources of one project."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.fixture = structure_fixtures.ProjectFixture()
        self.provider = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.provider.customer
        )
        self.completions = factories.OfferingMetricFactory(
            offering=self.offering,
            definition=factories.MetricDefinitionFactory(key="education.completions"),
        )
        self.response_time = factories.OfferingMetricFactory(
            offering=self.offering,
            project_aggregation=enums.ProjectAggregations.MEAN,
            definition=factories.MetricDefinitionFactory(
                key="support.response_time", kind=enums.MetricKinds.GAUGE
            ),
        )
        self.resources = [
            marketplace_factories.ResourceFactory(
                offering=self.offering,
                project=self.fixture.project,
                state=ResourceStates.OK,
            )
            for _ in range(2)
        ]
        self.now = timezone.now()
        for index, resource in enumerate(self.resources):
            for course, count in (("linux", 3 + index), ("gpu", 1)):
                self.add(
                    self.completions, resource, {"course": course}, count, minutes=5
                )
            self.add(self.response_time, resource, {}, 4 + 2 * index, minutes=5)
        rollups.roll_up()

    def add(self, offering_metric, resource, attributes, value, **ago):
        series, _ = models.MetricSeries.objects.get_or_create(
            resource=resource,
            offering_metric=offering_metric,
            attributes_hash=attributes_hash(attributes),
            defaults={"attributes": attributes},
        )
        models.MetricPoint.objects.create(
            series=series,
            timestamp=self.now - datetime.timedelta(**ago),
            value=value,
        )


class MetricSeriesTest(Scenario):
    def get(self, user, **params):
        self.client.force_authenticate(user)
        params.setdefault("start", (self.now - datetime.timedelta(days=1)).isoformat())
        return self.client.get(SERIES, params)

    def test_a_project_member_sees_the_project_total(self):
        response = self.get(
            self.fixture.member,
            offering_metric_uuid=self.completions.uuid.hex,
            project_uuid=self.fixture.project.uuid.hex,
            granularity="hour",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [group] = response.data["series"]
        self.assertEqual([p["value"] for p in group["points"]], [9])

    def test_breakdown_by_attribute(self):
        response = self.get(
            self.fixture.member,
            offering_metric_uuid=self.completions.uuid.hex,
            group_by="course",
            granularity="hour",
        )

        totals = {
            g["attributes"]["course"]: g["points"][0]["value"]
            for g in response.data["series"]
        }
        self.assertEqual(totals, {"linux": 7, "gpu": 2})

    def test_a_level_is_averaged_across_resources(self):
        response = self.get(
            self.fixture.member,
            offering_metric_uuid=self.response_time.uuid.hex,
            granularity="hour",
        )

        self.assertEqual(response.data["series"][0]["points"][0]["value"], 5)

    def test_auto_picks_raw_points_for_a_short_range(self):
        response = self.get(
            self.provider.owner, offering_metric_uuid=self.completions.uuid.hex
        )

        self.assertEqual(response.data["granularity"], enums.Granularities.RAW)

    def test_a_member_of_another_project_sees_nothing(self):
        response = self.get(
            structure_factories.UserFactory(),
            offering_metric_uuid=self.completions.uuid.hex,
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_grouping_by_an_undeclared_attribute_is_refused(self):
        response = self.get(
            self.fixture.member,
            offering_metric_uuid=self.completions.uuid.hex,
            group_by="teacher",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProjectMetricsTest(Scenario):
    def summary(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            PROJECT_METRICS, {"project_uuid": self.fixture.project.uuid.hex}
        )

    def test_figures_and_goal_status(self):
        models.MetricGoal.objects.create(
            offering_metric=self.completions,
            value=5,
            comparator=enums.Comparators.AT_LEAST,
        )
        models.MetricGoal.objects.create(
            offering_metric=self.response_time,
            project=self.fixture.project,
            value=4,
            comparator=enums.Comparators.AT_MOST,
        )

        response = self.summary(self.fixture.member)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_key = {item["offering_metric"]["key"]: item for item in response.data}
        completions = by_key["education.completions"]
        self.assertEqual(completions["current"], 9)
        self.assertTrue(completions["goal_met"])
        self.assertFalse(completions["goal_is_project"])
        response_time = by_key["support.response_time"]
        self.assertEqual(response_time["current"], 5)
        self.assertFalse(response_time["goal_met"])
        self.assertTrue(response_time["goal_is_project"])

    def test_an_outsider_cannot_read_a_projects_figures(self):
        response = self.summary(structure_factories.UserFactory())

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_projects_expose_has_metrics(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            structure_factories.ProjectFactory.get_url(self.fixture.project)
        )

        self.assertTrue(response.data["has_metrics"])


class MetricBreakdownTest(Scenario):
    def test_a_gauge_counts_every_resource_not_only_the_newest_bucket(self):
        learners = factories.OfferingMetricFactory(
            offering=self.offering,
            definition=factories.MetricDefinitionFactory(
                key="education.learners.active",
                kind=enums.MetricKinds.GAUGE,
                attribute_keys=["course"],
            ),
        )
        # Two resources report the same course in different hours.
        self.add(learners, self.resources[0], {"course": "linux"}, 10, minutes=5)
        self.add(learners, self.resources[1], {"course": "linux"}, 20, hours=3)
        self.add(learners, self.resources[1], {"course": "gpu"}, 4, hours=3)
        rollups.roll_up()
        self.client.force_authenticate(self.fixture.member)

        response = self.client.get(
            "/api/marketplace-metric-breakdown/",
            {
                "offering_metric_uuid": learners.uuid.hex,
                "project_uuid": self.fixture.project.uuid.hex,
                "group_by": "course",
                "start": (self.now - datetime.timedelta(days=1)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        figures = {item["value"]: item["figure"] for item in response.data}
        self.assertEqual(figures, {"linux": 30, "gpu": 4})

    def test_an_outsider_sees_nothing(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get(
            "/api/marketplace-metric-breakdown/",
            {
                "offering_metric_uuid": self.completions.uuid.hex,
                "project_uuid": self.fixture.project.uuid.hex,
                "group_by": "course",
                "start": self.now.isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MetricGoalTest(Scenario):
    def create(self, user, **payload):
        self.client.force_authenticate(user)
        return self.client.post(GOALS, payload, format="json")

    def test_a_project_manager_sets_the_projects_goal(self):
        response = self.create(
            self.fixture.manager,
            offering_metric=self.completions.uuid.hex,
            project=self.fixture.project.uuid.hex,
            value=12,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_the_provider_sets_the_default_goal(self):
        response = self.create(
            self.provider.owner, offering_metric=self.completions.uuid.hex, value=10
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_a_project_manager_cannot_set_the_default_goal(self):
        response = self.create(
            self.fixture.manager, offering_metric=self.completions.uuid.hex, value=10
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_outsider_learns_nothing_about_the_project(self):
        other = factories.OfferingMetricFactory()

        response = self.create(
            structure_factories.UserFactory(),
            offering_metric=other.uuid.hex,
            project=self.fixture.project.uuid.hex,
            value=1,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn(self.fixture.project.name, str(response.data))

    def test_a_project_not_using_the_offering_cannot_set_a_goal(self):
        response = self.create(
            self.fixture.manager,
            offering_metric=factories.OfferingMetricFactory().uuid.hex,
            project=self.fixture.project.uuid.hex,
            value=1,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_one_goal_per_scope(self):
        payload = {
            "offering_metric": self.completions.uuid.hex,
            "project": self.fixture.project.uuid.hex,
            "value": 12,
        }
        self.create(self.fixture.manager, **payload)

        response = self.create(self.fixture.manager, **payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PurgeTest(Scenario):
    @mock.patch("waldur_mastermind.marketplace_metrics.tasks.purge_offering_metric")
    def test_only_an_archived_metric_can_be_purged(self, purge_task):
        self.client.force_authenticate(self.provider.owner)
        url = factories.OfferingMetricFactory.get_url(self.completions, "purge")

        self.assertEqual(self.client.post(url).status_code, status.HTTP_409_CONFLICT)

        self.completions.state = enums.OfferingMetricStates.ARCHIVED
        self.completions.save()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        purge_task.delay.assert_called_once_with(self.completions.uuid.hex)


class ReviewFixesReadTest(Scenario):
    def test_a_gauge_sums_resources_that_report_at_different_times(self):
        learners = factories.OfferingMetricFactory(
            offering=self.offering,
            definition=factories.MetricDefinitionFactory(
                key="education.learners.active",
                kind=enums.MetricKinds.GAUGE,
                attribute_keys=[],
            ),
        )
        # Resource 0 reports at -3h and -1h, resource 1 only at -2h.
        self.add(learners, self.resources[0], {}, 10, hours=3)
        self.add(learners, self.resources[1], {}, 10, hours=2)
        self.add(learners, self.resources[0], {}, 12, hours=1)
        rollups.roll_up()
        self.client.force_authenticate(self.fixture.member)

        for granularity in ("raw", "hour"):
            response = self.client.get(
                SERIES,
                {
                    "offering_metric_uuid": learners.uuid.hex,
                    "start": (self.now - datetime.timedelta(hours=4)).isoformat(),
                    "granularity": granularity,
                },
            )
            values = [p["value"] for p in response.data["series"][0]["points"]]
            self.assertEqual(values, [10, 20, 22], granularity)

    def test_a_period_beyond_hourly_retention_reads_daily_rollups(self):
        policy = models.RetentionPolicy.objects.create(
            name="short-hourly", raw_days=8, hourly_days=8
        )
        definition = self.completions.definition
        definition.retention_policy = policy
        definition.save()
        series = models.MetricSeries.objects.filter(
            offering_metric=self.completions
        ).first()
        old_day = (self.now - datetime.timedelta(days=20)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        models.MetricRollup.objects.create(
            series=series,
            granularity=enums.Granularities.DAY,
            bucket_start=old_day,
            count=1,
            sum=100,
            min=100,
            max=100,
            last=100,
            last_timestamp=old_day,
        )
        figure = query.period_figure(
            self.completions,
            [series.id],
            self.now - datetime.timedelta(days=30),
            self.now,
        )

        self.assertEqual(figure, 100 + series.points.get().value)

    def test_the_default_policy_cannot_be_removed_or_renamed(self):
        self.client.force_authenticate(self.fixture.staff)
        policy = models.RetentionPolicy.objects.get(name="standard")
        url = f"/api/marketplace-metric-retention-policies/{policy.uuid.hex}/"

        self.assertEqual(self.client.delete(url).status_code, status.HTTP_409_CONFLICT)
        response = self.client.patch(url, {"name": "other"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_archived_metric_cannot_get_a_goal(self):
        self.completions.state = enums.OfferingMetricStates.ARCHIVED
        self.completions.save()
        self.client.force_authenticate(self.fixture.manager)

        response = self.client.post(
            GOALS,
            {
                "offering_metric": self.completions.uuid.hex,
                "project": self.fixture.project.uuid.hex,
                "value": 5,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ServiceProviderManagerTest(Scenario):
    """The provider's service provider manager, whose role sits on ServiceProvider."""

    def setUp(self):
        super().setUp()
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        provider = marketplace_factories.ServiceProviderFactory(
            customer=self.provider.customer
        )
        self.manager = structure_factories.UserFactory()
        provider.add_user(self.manager, ServiceProviderRole.MANAGER)
        self.client.force_authenticate(self.manager)

    def test_sees_every_consumers_series(self):
        response = self.client.get(
            SERIES,
            {
                "offering_metric_uuid": self.completions.uuid.hex,
                "start": (self.now - datetime.timedelta(days=1)).isoformat(),
                "granularity": "hour",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [group] = response.data["series"]
        self.assertEqual([p["value"] for p in group["points"]], [9])

    def test_sets_and_lists_the_default_goal(self):
        response = self.client.post(
            GOALS,
            {"offering_metric": self.completions.uuid.hex, "value": 10},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        listed = self.client.get(GOALS).data
        self.assertIn(response.data["uuid"], {goal["uuid"] for goal in listed})


class ResourceDrillDownTest(Scenario):
    """Per resource: completions are linux 3 + gpu 1 and linux 4 + gpu 1."""

    def breakdown(self, user, **params):
        self.client.force_authenticate(user)
        return self.client.get(
            "/api/marketplace-metric-breakdown/",
            {
                "offering_metric_uuid": self.completions.uuid.hex,
                "start": (self.now - datetime.timedelta(days=1)).isoformat(),
                **params,
            },
        )

    def summary(self, user, resource):
        self.client.force_authenticate(user)
        return self.client.get(
            "/api/marketplace-resource-metrics/",
            {"resource_uuid": resource.uuid.hex},
        )

    def test_a_project_figure_breaks_down_by_resource(self):
        response = self.breakdown(
            self.fixture.member,
            project_uuid=self.fixture.project.uuid.hex,
            group_by="resource",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        figures = {item["resource_uuid"]: item["figure"] for item in response.data}
        self.assertEqual(
            figures,
            {
                str(self.resources[0].uuid): 4,
                str(self.resources[1].uuid): 5,
            },
        )
        names = {item["resource_name"] for item in response.data}
        self.assertEqual(names, {r.name for r in self.resources})

    def test_a_resource_figure_breaks_down_by_attribute(self):
        response = self.breakdown(
            self.fixture.member,
            resource_uuid=self.resources[1].uuid.hex,
            group_by="course",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        figures = {item["value"]: item["figure"] for item in response.data}
        self.assertEqual(figures, {"linux": 4, "gpu": 1})

    def test_a_resource_figure_does_not_break_down_by_resource(self):
        response = self.breakdown(
            self.fixture.member,
            resource_uuid=self.resources[1].uuid.hex,
            group_by="resource",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_resource_reports_its_own_figures(self):
        response = self.summary(self.fixture.member, self.resources[1])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        figures = {
            item["offering_metric"]["key"]: item["current"] for item in response.data
        }
        self.assertEqual(
            figures, {"education.completions": 5, "support.response_time": 6}
        )
        self.assertNotIn("goal", response.data[0])

    def test_the_provider_sees_a_consumers_resource(self):
        response = self.summary(self.provider.owner, self.resources[0])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_an_outsider_sees_neither(self):
        outsider = structure_factories.UserFactory()

        self.assertEqual(
            self.summary(outsider, self.resources[0]).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.breakdown(
                outsider,
                resource_uuid=self.resources[0].uuid.hex,
                group_by="course",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_resource_of_another_offering_is_not_found(self):
        other = marketplace_factories.ResourceFactory(
            project=self.fixture.project, state=ResourceStates.OK
        )

        response = self.breakdown(
            self.fixture.member, resource_uuid=other.uuid.hex, group_by="course"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


def _utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.UTC)


class PeriodBoundsTest(test.APITestCase):
    def test_a_month_so_far_is_compared_with_the_same_days_of_the_last(self):
        now = _utc(2026, 10, 5, 6)

        start, end, previous_start, previous_end = query.period_bounds(
            enums.GoalPeriods.MONTH, now
        )

        self.assertEqual((start, end), (_utc(2026, 10, 1), now))
        self.assertEqual(
            (previous_start, previous_end), (_utc(2026, 9, 1), _utc(2026, 9, 5, 6))
        )

    def test_a_long_month_never_reads_past_a_short_one(self):
        _, _, previous_start, previous_end = query.period_bounds(
            enums.GoalPeriods.MONTH, _utc(2026, 3, 31, 12)
        )

        self.assertEqual(
            (previous_start, previous_end), (_utc(2026, 2, 1), _utc(2026, 3, 1))
        )

    def test_a_quarter_so_far(self):
        _, _, previous_start, previous_end = query.period_bounds(
            enums.GoalPeriods.QUARTER, _utc(2026, 10, 15)
        )

        self.assertEqual(
            (previous_start, previous_end), (_utc(2026, 7, 1), _utc(2026, 7, 15))
        )

    def test_a_rolling_window_compares_two_whole_windows(self):
        now = _utc(2026, 10, 5)

        start, end, previous_start, previous_end = query.period_bounds(
            enums.GoalPeriods.ROLLING_30_DAYS, now
        )

        self.assertEqual(end - start, previous_end - previous_start)
        self.assertEqual(previous_end, start)


@freeze_time("2026-10-05 06:00:00")
class PeriodComparisonTest(Scenario):
    def test_previous_figure_covers_the_same_days_of_the_last_month(self):
        jobs = factories.OfferingMetricFactory(
            offering=self.offering,
            definition=factories.MetricDefinitionFactory(key="hpc.jobs"),
        )
        resource = self.resources[0]
        for when, value in (
            (_utc(2026, 9, 3, 12), 4),  # inside 1-5 September, 06:00
            (_utc(2026, 9, 20), 100),  # later in September: not compared
            (_utc(2026, 10, 2), 6),  # this month
        ):
            self.add(
                jobs, resource, {}, value, seconds=(self.now - when).total_seconds()
            )
        rollups.roll_up(since=_utc(2026, 8, 1))
        self.client.force_authenticate(self.fixture.member)

        response = self.client.get(
            PROJECT_METRICS, {"project_uuid": self.fixture.project.uuid.hex}
        )

        item = next(
            i for i in response.data if i["offering_metric"]["key"] == "hpc.jobs"
        )
        self.assertEqual((item["current"], item["previous"]), (6, 4))
